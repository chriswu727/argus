"""Argus MCP Server — exposes browser testing tools to Claude Code.

Claude Code becomes the AI planner. Argus provides the browser, DOM
extraction, error capture, and report generation. No API key needed.

Usage in Claude Code settings (~/.claude/settings.json):
    {
        "mcpServers": {
            "argus": {
                "command": "argus-mcp"
            }
        }
    }

Then in Claude Code just say:
    "Test my app at http://localhost:3000, focus on the login flow"
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from mcp.server.fastmcp import FastMCP

from .browser import BrowserDriver, _redact, _redact_headers
from .detector import Detector
from .differ import compute_changes
from .models import Bug, BugType, ExplorationResult, InteractiveElement, PageState, Screenshot, Severity
from .reporter import Reporter
from .resolver import (
    describe as describe_element,
    describe_screen as describe_screen_element,
    resolve_element,
    resolve_screen_element,
)

mcp = FastMCP(
    "argus",
    instructions="""You are now Argus, the all-seeing QA tester for a software product.
While this MCP is loaded you are not a coding assistant, not a task
completer, not the user's friend. You are a senior human QA tester sitting
down at the user's machine with one job: find the bugs the dev team would
be embarrassed to ship. Stay in role until end_session is called.

HOW YOU WORK — this is the whole point
You actually open the software and USE it, like a real person trying to get
something done. You don't audit it from the outside, you don't read its
source to find flaws, you don't fire scripted probes at it. You drive it
through real journeys with real intent — and you notice what breaks, looks
wrong, or betrays the user's trust while you're using it. Black-box, from
the user's side of the screen. The deepest bugs (cross-page state drift,
silent data loss, deceptive feedback) only surface when you carry real
state through a real journey — never from poking one surface in isolation.

GOAL
Quality of bugs found matters more than quantity. A tight 5-bug report
beats a noisy 50-bug report. Ship findings a real user would care about,
not theoretical ones.

NOT YOUR JOB
- Completing the user's flow as if you were a real user (don't actually
  buy the thing, don't actually publish the post — unless the act itself
  is what you're testing).
- Suggesting code fixes — you are QA, not dev.
- SEO / Lighthouse-style performance audits / generic accessibility
  scans — those are different products. Use axe / Lighthouse for those.
  You only flag a11y or perf when it makes the app unusable for a real user.
- Mechanically firing every payload from a security textbook. A real
  tester picks one well-chosen probe per surface, observes, moves on.

THE TESTER'S RITUAL — return to this on every tool call

1. MAP         What does this app let a user do? Identify the 3-5 real
               user goals (sign up, add an item and find it later, check
               out, edit a setting and see it stick) before anything else.
2. USE IT      Pick a goal and actually walk it end-to-end, the way a real
               person would — not by poking surfaces, but by genuinely
               trying to accomplish the goal. Carry state across pages as
               you go (the name you entered, the item you added). Most of
               the bugs worth reporting reveal themselves mid-journey.
3. HYPOTHESIZE As you use each surface, name 2-3 specific ways it could
               fail. Not "the form might break" — "I bet validation runs
               only client-side and the server accepts garbage".
4. ACT         One probe per tool call. Resist testing five things in one
               click — a real tester does one thing, then watches.
5. OBSERVE     After every action, read what came back: state diff,
               console, network, visible feedback. Compare expected vs
               actual. Take a screenshot when something looks off.
6. VERIFY      For any destructive or persistence-changing action
               (delete, save, edit, submit, toggle, payment), call
               verify_persistence. UIs lie. The "Saved!" toast is the
               single most common reason real users lose data.
7. RECORD      When you've confirmed a real bug, call record_bug with
               severity + reproducible steps + evidence. ALWAYS pass a
               `verify` clause — {expect: "present"|"absent", target_text:
               "the text that proves the bug", at_url: "/where"} — for any
               text-checkable symptom (an item present/absent, a wrong count,
               a lying toast, missing saved data). Without it the finding has
               NO reproduction receipt and counts as unproven say-so; that is
               the whole differentiator. Only skip verify for a purely visual
               judgment call. Don't record speculation or polish nits.
8. COVER       Before ending the session, ask "which user goals did I
               never actually use end-to-end?" — go use those.

WHAT MAKES A REAL BUG (the bar)
- Reproducible — someone following your steps will see it too.
- User-affecting — causes data loss, security risk, blocked flow, real
  confusion, or trust damage.
- Persistent — not a one-off page-load race unless you can re-trigger it.

THE THINGS HUMANS NOTICE THAT MACHINES MISS — your hunting ground
- The success toast is a lie — operation didn't actually persist.
- Cross-page state inconsistency — same datum displayed differently
  across pages, or one page updates and another doesn't.
- Empty states aren't designed (says "Loading..." forever, or just blank).
- Long values silently truncated with no indication.
- Validation messages are engineer-speak, not user-speak ("Field 'foo'
  invalid" — what is foo, what should I do?).
- A workflow has no back / cancel / recover path — user is trapped.
- Visual hierarchy inverted — destructive button is the prominent one;
  primary CTA is the dim one.
- Dark patterns — fake urgency, hidden cost, hard-to-cancel, deceptive
  consent, sneaky charges.
- After auth, navigation/UI doesn't reflect logged-in state.
- Form errors clear the user's input — they have to retype everything.
- The same action via two paths gives different results.
- Inputs accept what should be rejected (auth bypass, bypassed
  validation, accepted out-of-range numbers, accepted whitespace where
  content is required).

SEVERITY CALIBRATION
- HIGH    data loss, security, payment, blocked primary flow
- MEDIUM  workflow friction, confusing UX, deceptive feedback,
          cross-page inconsistency
- LOW     polish, copy, suggestion-grade

OPERATING RHYTHM
After every tool call, ask yourself: was that a tester move, or did I
slip into being a regular user / a developer / a wandering agent? If
you slipped, return to the ritual. The MCP is loaded specifically so
you stay in the tester seat — use that.""",
)


class Session:
    """Holds the state for one testing session.

    A session has a `mode` of either "web" (BrowserDriver) or "screen"
    (ScreenBackend). Tools that span both modes (record_bug,
    end_session) check `mode`; tools that are mode-specific (observe
    vs screen_observe) live as separate tools so the contract stays
    honest about what each mode can do.
    """

    def __init__(self):
        self.mode: Optional[str] = None  # "web" | "screen" | None
        self.browser: Optional[BrowserDriver] = None
        self.screen = None  # type: Optional["ScreenBackend"]
        self.detector = Detector()
        self.bugs: List[Bug] = []
        self.steps: List[str] = []
        # Index into self.steps marking where the *last* record_bug call
        # snapshotted from. Each Bug's reproducible steps are the delta
        # since this cursor, so consecutive bug reports don't accumulate
        # earlier bugs' actions in their repro steps.
        self._steps_since_last_bug: int = 0
        self.pages_visited: List[str] = []
        self.screenshots: List[Screenshot] = []
        self.start_time: Optional[float] = None
        self.url: Optional[str] = None
        self.focus_areas: List[str] = []
        self._last_elements = []
        self._last_screen_elements = []
        self._screenshot_counter = 0
        # Structured, replayable action trace (parallel to the human-readable
        # self.steps). Each entry: {tool, description, value}. record_bug slices
        # the actions since the previous bug onto Bug.replay_steps.
        self.action_trace: List[dict] = []
        self._actions_since_last_bug: int = 0
        # Liveness marker of the most recently restored state capsule (or None).
        # record_bug re-checks it against the CURRENT page so a finding made
        # while the capsule's server session is dead gets flagged — and one made
        # after re-minting state through the UI does NOT (no sticky latch).
        self._capsule_marker: Optional[str] = None
        # Lazy-initialised screen-mode safety state. Populated by
        # start_screen_session; consulted by every screen-mode tool.
        self._safety = None  # type: Optional["argus.screen.safety.SafetyState"]

    @property
    def active(self) -> bool:
        return self.browser is not None or self.screen is not None


_session = Session()


def _require_session() -> Session:
    if not _session.active:
        raise RuntimeError(
            "No active session. Call start_session(url) for web mode "
            "or start_screen_session() for screen mode first."
        )
    return _session


def _require_web_session(s: "Session", tool: str) -> Optional[str]:
    """Return an error string if this isn't a live web session, else None.

    Replaces the `if s.mode != "web" or s.browser is None: return "..."` guard
    that was copy-pasted across ~30 tools (the duplication that once let a
    missing guard slip through). Tools that also need an open page keep their
    explicit `_page is None` check."""
    if s.mode != "web" or s.browser is None:
        return f"{tool}: this tool is web-mode only."
    return None


async def _teardown_active_session() -> None:
    """Cleanly stop whichever backend the current session holds.

    Both start_session and start_screen_session switch modes; tearing down
    only `.browser` crashed when the prior session was screen-mode
    (None.stop()), and only `.screen` would leak the browser. Stop whatever
    is set, best-effort.
    """
    if not _session.active:
        return
    if _session.browser is not None:
        try:
            await _session.browser.stop()
        except Exception:
            pass
    if _session.screen is not None:
        try:
            await _session.screen.stop()
        except Exception:
            pass


def _record_action(s: "Session", tool: str, description: str = "", value: Optional[str] = None) -> None:
    """Append a structured, replayable action to the session trace."""
    s.action_trace.append({"tool": tool, "description": description, "value": value})


def _output_dir() -> str:
    return os.environ.get("ARGUS_OUTPUT_DIR", "./argus-reports")


# ── persistent test journal (cross-run regression) ──────────────────

def _journal_path(origin: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", origin or "default")
    return Path(_output_dir()) / ".argus" / "journal" / f"{safe}.json"


def _bug_fingerprint(bug: "Bug") -> str:
    """Stable identity for cross-run dedup — from the receipt's structural fields
    (type + url path + expect + target), never the LLM-authored title, so a
    genuinely new bug is never silently collapsed into an old one."""
    r = bug.reproduction_receipt or {}
    path = urlparse(r.get("at_url") or bug.url or "").path or "/"
    return f"{bug.type.value}|{path}|{r.get('expect', '')}|{(r.get('target_text') or '')[:80]}"


def _journal_entries(origin: str) -> list:
    path = _journal_path(origin)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _write_journal(s: "Session") -> None:
    """Persist re-checkable findings (those with a clean-load verify clause) so a
    later run can re-test them. Replay-mode receipts are skipped — re-driving
    them re-executes writes, so they're not safe to auto-regression-check."""
    origin = urlparse(s.url or "").netloc or "default"
    fresh = []
    for b in s.bugs:
        r = b.reproduction_receipt or {}
        if r.get("mode") == "replay":
            continue
        expect, target = r.get("expect"), r.get("target_text")
        # Only journal findings Argus actually CONFIRMED (reproduced). A
        # not-reproduced / errored finding would otherwise resurface next run as
        # a phantom "no-longer-reproduces (likely fixed)".
        if r.get("reproduced") is True and expect in ("present", "absent") and target:
            fresh.append({
                "fingerprint": _bug_fingerprint(b),
                "title": b.title[:120], "severity": b.severity.value, "type": b.type.value,
                "verify": {"expect": expect, "target_text": target, "at_url": r.get("at_url", "")},
            })
    if not fresh:
        return
    merged = {e["fingerprint"]: e for e in _journal_entries(origin)}
    for e in fresh:
        merged[e["fingerprint"]] = e
    path = _journal_path(origin)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gi = path.parent / ".gitignore"  # journals embed app text — keep out of git
        if not gi.exists():
            gi.write_text("*\n")
        path.write_text(json.dumps(list(merged.values()), indent=2))
    except Exception:
        pass


async def _auto_screenshot(s: Session, name: str, step: str) -> str:
    """Take a screenshot and register it in the session.

    Mode-aware: web-mode goes through Playwright; screen-mode shells
    out to `screencapture`. Both produce a PNG at the same output
    location and append a Screenshot record with the correct URL.
    """
    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")

    if s.mode == "web" and s.browser is not None:
        await s.browser.screenshot(path)
        url = s.browser._page.url if s.browser._page else ""
    elif s.mode == "screen" and s.screen is not None:
        # Shell out for the same convention used elsewhere.
        import subprocess as _sp
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            _sp.run(["screencapture", "-t", "png", "-x", path], capture_output=True, timeout=5, check=True)
        except Exception:
            pass
        url = f"screen://{s.screen._app_name or 'unknown'}"
    else:
        url = ""

    s.screenshots.append(Screenshot(
        path=path, name=safe_name, step=step, url=url,
    ))
    return path


def _resolve_url(s: "Session", url: str) -> str:
    """Resolve a possibly-relative URL against the current page.

    Playwright's goto rejects bare paths like "/tasks"; agents (and our own
    docstrings) routinely pass them. Join against the live page origin.
    """
    if url.startswith(("http://", "https://")):
        return url
    base = ""
    if s.browser is not None and s.browser._page is not None:
        base = s.browser._page.url
    if not url:
        return base
    return urljoin(base, url) if base else url


def _token_present(needle: str, haystack: str) -> bool:
    """Whitespace-normalised, word-boundary containment.

    A bare substring scan lets a claimed symptom match incidentally — the
    target living inside a longer word ('cat' in 'category', 'Delete' in
    'Deleted') — which would stamp a VERIFIED receipt on a non-bug. Require
    the needle to sit on word boundaries so only a token-level occurrence
    counts. This does not prove the occurrence is *the* one claimed, but it
    kills the mid-word / bleed-through false positives the receipt must avoid.
    """
    needle = " ".join(needle.lower().split())
    haystack = " ".join(haystack.lower().split())
    if not needle:
        return False
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
    return re.search(pattern, haystack) is not None


def _text_in_state(text: str, state: PageState) -> bool:
    """Check if text is present as a token in page_text, elements, or item_lists."""
    if not text.strip():
        return False
    if _token_present(text, state.page_text):
        return True
    for el in state.elements:
        if el.text and _token_present(text, el.text):
            return True
        if el.value and _token_present(text, el.value):
            return True
    for items in state.item_lists.values():
        for item in items:
            if _token_present(text, item):
                return True
    return False


def _marker_visible(text: str, state: PageState) -> bool:
    """Like _text_in_state but VISIBLE text only — never matches an input's
    `value`. A liveness marker (a user's name/email) is commonly pre-filled into
    a logged-out login form's value, which would falsely read as 'logged in'.
    """
    if not text.strip():
        return False
    if _token_present(text, state.page_text):
        return True
    for el in state.elements:
        if el.text and _token_present(text, el.text):
            return True
    return False


def _visible_text_in_state(text: str, state: PageState) -> bool:
    """Visible-content match: page text, element text, and list rows — but NOT
    input `value` (the replay just typed into those; matching them would certify
    a symptom off the text the replay itself entered)."""
    if not text.strip() or state is None:
        return False
    if _token_present(text, state.page_text):
        return True
    for el in state.elements:
        if el.text and _token_present(text, el.text):
            return True
    for items in state.item_lists.values():
        for row in items:
            if _token_present(text, row):
                return True
    return False


_EXPECT_KEYS = {"count", "gains", "removes", "text_present", "text_absent", "toast", "url_changed"}


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def _evaluate_expectation(before: PageState, after: PageState, expect: dict) -> list:
    """Predict-then-check: evaluate a bounded predicate dict against the
    before/after diff. Returns [(label, ok, detail)] where ok is:
      True  = prediction held, False = SURPRISE (a bug lead),
      None  = could not evaluate (unmeasurable) — NEVER a surprise.

    The vocabulary is deliberately small — only the deltas the page state
    already exposes — and judged HONESTLY so neither a false MATCH nor a false
    SURPRISE can mislead the tester:
      - gains/removes use set membership across whole before/after row sets
        (a token present after but not before = a real add), so an in-place
        row edit (priority/timestamp change) is NOT mistaken for an add+remove.
      - text checks use VISIBLE text only (never an input value) and require
        the text to have APPEARED (text_present), not merely pre-exist.
      - an unmeasurable count (label not on the page) or an unknown key is
        reported as UNCHECKED, not as a failed prediction.
    Token matching is substring-on-word-boundary, so pass a distinctive item
    text to avoid bleed onto a sibling row.
    """
    results = []

    def _as_list(x):
        return [x] if isinstance(x, str) else list(x or [])

    def _in(item, state):
        if _token_present(item, state.page_text):
            return True
        for el in state.elements:
            if el.text and _token_present(item, el.text):
                return True
        for items in state.item_lists.values():
            for row in items:
                if _token_present(item, row):
                    return True
        return False

    c = expect.get("count")
    if c is not None:
        if not (isinstance(c, dict) and c.get("label")):
            results.append(("count (malformed — need {label, delta|value})", None, repr(c)[:60]))
        else:
            lab = c["label"]
            b, a = before.counts.get(lab), after.counts.get(lab)
            if a is None and b is None:
                results.append((f"count {lab!r}", None, "label not among page counts — could not evaluate"))
            elif "delta" in c:
                try:
                    want = int(c["delta"])
                    actual = (a or 0) - (b or 0)
                    results.append((f"count {lab!r} change {want:+d}", actual == want, f"{b} -> {a}"))
                except (TypeError, ValueError):
                    results.append((f"count {lab!r} delta", None, f"non-numeric delta {c['delta']!r}"))
            elif "value" in c:
                results.append((f"count {lab!r} == {c['value']}", a == c["value"], f"observed {a}"))
            else:
                results.append((f"count {lab!r} (malformed — need delta or value)", None, ""))

    for item in _as_list(expect.get("gains")):
        after_has, before_has = _in(item, after), _in(item, before)
        ok = after_has and not before_has
        detail = "newly present" if ok else ("already present before action" if before_has else "not present after")
        results.append((f"list gains {item!r}", ok, detail))
    for item in _as_list(expect.get("removes")):
        after_has, before_has = _in(item, after), _in(item, before)
        ok = before_has and not after_has
        detail = "gone now" if ok else ("STILL present" if after_has else "was not present before action")
        results.append((f"list removes {item!r}", ok, detail))

    if "text_present" in expect:
        t = expect["text_present"]
        ok = _in(t, after) and not _in(t, before)
        detail = "appeared" if ok else ("already there before action" if _in(t, before) else "not visible after")
        results.append((f"text appears {t!r}", ok, detail))
    if "text_absent" in expect:
        t = expect["text_absent"]
        ok = not _in(t, after)
        results.append((f"text absent {t!r}", ok, "absent" if ok else "STILL visible"))
    if "toast" in expect:
        t = expect["toast"]
        new_toasts = set(after.toast_messages) - set(before.toast_messages)
        ok = any(_token_present(t, tm) for tm in new_toasts)
        results.append((f"toast {t!r}", ok, "; ".join(list(new_toasts)[:2]) or "no new toast"))
    if "url_changed" in expect:
        want = _as_bool(expect["url_changed"])
        changed = before.url != after.url
        results.append((f"url changed == {want}", changed == want, f"{before.url} -> {after.url}"))

    for k in expect:
        if k not in _EXPECT_KEYS:
            results.append((f"unknown key {k!r}", None, "not a recognised predicate — ignored"))

    return results


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _reconcile_action(new_reqs: list, before: PageState, after: PageState) -> tuple:
    """Surface the network a click produced as EVIDENCE for the agent to judge —
    deliberately NOT an auto-verdict. Returns (evidence_lines, check_or_None).

    Argus does not assert "deceptive success" from heuristics: request->action
    attribution is fuzzy (background polls / telemetry land in the same window),
    "no network write" is legitimate for optimistic UI and client-side/offline
    persistence, and success/failure copy is open-vocabulary. So it shows what
    fired (methods, statuses, errors) and, when a message appeared, nudges the
    agent to CONFIRM the claim persisted — the reliable oracle is a fresh
    reload, not the wire. This is the seam pure-FE tests (never see persistence)
    and pure-BE tests (never see the UI claim) both miss; the agent makes the
    call.
    """
    mutating = [r for r in new_reqs if (r.get("method") or "").upper() in _MUTATING_METHODS]
    failed = [r for r in new_reqs if (r.get("status") or 0) >= 400]
    new_toasts = set(after.toast_messages) - set(before.toast_messages)

    evidence = [f"Requests fired by this action: {len(new_reqs)} ({len(mutating)} mutating)"]
    for r in new_reqs[:6]:
        st = r.get("status")
        evidence.append(f"  {(r.get('method') or '?'):6} "
                        f"{(str(st) if st is not None else 'pending'):>7}  {_redact(r.get('url') or '')[:90]}")
    if len(new_reqs) > 6:
        evidence.append(f"  (+{len(new_reqs) - 6} more)")
    for r in failed:
        evidence.append(f"  error: {r.get('method')} {_redact(r.get('url') or '')[:70]} -> HTTP {r.get('status')}")

    check = None
    if new_toasts:
        names = "; ".join(list(new_toasts)[:2])[:80]
        if not mutating:
            check = (f"a message appeared ({names!r}) but this click fired NO mutating request. "
                     "If it claims a saved/changed state, confirm it actually persisted on a fresh "
                     "reload (verify_persistence) — optimistic UI / client-side persistence are "
                     "legitimate, a lying toast is not.")
        elif failed:
            check = (f"a message appeared ({names!r}) and a request this click fired returned an "
                     "error (above). If the message claims success, that may be success copy over a "
                     "failed write — read the response body (network_request) or reload to confirm.")
    return evidence, check


_LOGOUT_PHRASES = ("please log in", "please sign in", "log in to", "sign in to",
                   "session expired", "session has expired", "you have been logged out",
                   "you are logged out", "log in to continue", "sign in to continue")


def _looks_logged_out(state: PageState) -> bool:
    """Heuristic: the page is a login wall (so an auth-gated symptom is absent
    because we're logged out, not because the bug was fixed). Conservative full
    phrases, not the bare word 'login' (which appears on logged-in pages too)."""
    t = (state.page_text or "").lower()
    return any(p in t for p in _LOGOUT_PHRASES)


async def _run_reproduction_check(s: "Session", verify: dict) -> dict:
    """Independently re-confirm a bug's observable symptom from a clean load.

    The agent's current page may be stale or reflect a hallucinated element.
    This re-navigates (fresh GET, no cached DOM) to the relevant URL and
    checks present/absent against the agent's claim — twice. Two consecutive
    loads catch a symptom that flips between them (marked intermittent), but
    they are close together, so a genuine timing/race symptom can read a
    steady 2/2 or 0/2; treat the receipt as confirming fresh-load presence,
    not as a flakiness measurement.
    We do NOT reset the fixture: that would wipe the agent's live session.
    "Clean load" here is a fresh same-context reload — it re-reads
    server-backed state (cookies/session persist, so auth survives) and any
    active request mocks are suspended for the check. It does NOT clear
    localStorage/sessionStorage: a symptom that lives purely in client web
    storage can survive the reload, so an `expect=absent` claim against
    client-only state may read present. This is the right boundary for the
    common case (don't log the agent out), but it means the receipt proves
    "server-backed truth", not "all client state cleared".

    Returns a receipt dict; never raises.
    """
    expect = (verify.get("expect") or "").strip().lower()
    target = (verify.get("target_text") or verify.get("target") or "").strip()
    at_url = (verify.get("at_url") or verify.get("after_url") or "").strip()

    if expect not in ("present", "absent") or not target:
        return {"attempted": False,
                "reason": "verify needs expect in {present, absent} and a non-empty target_text"}
    if s.mode != "web" or s.browser is None or s.browser._page is None:
        return {"attempted": False,
                "reason": "reproduction re-check is only available in web mode"}

    restore_url = s.browser._page.url
    nav_url = _resolve_url(s, at_url) if at_url else restore_url
    observations = []
    # A clean load means none of the agent's own forced responses are live —
    # otherwise an injected 500 re-fires on reload and certifies itself.
    suspended_mocks = await s.browser.suspend_mocks()
    inconclusive = None
    try:
        for _ in range(2):
            resp = await s.browser.goto(nav_url)
            state = await s.browser.get_state()
            # A 4xx/5xx page or a login wall makes the symptom absent for the
            # wrong reason — never certify (esp. expect=absent) off that.
            if resp is not None and resp.status >= 400:
                inconclusive = f"clean load returned HTTP {resp.status} — can't attribute the symptom"
                break
            if _looks_logged_out(state):
                inconclusive = "clean load hit a login wall — the session may have expired"
                break
            observations.append(_text_in_state(target, state))
    except Exception as e:  # navigation/render failure — report, don't crash record_bug
        return {"attempted": True, "reproduced": None, "at_url": nav_url,
                "error": str(e)[:200], "mocks_suspended": suspended_mocks}
    finally:
        await s.browser.restore_mocks(suspended_mocks)
        # Best-effort: return the agent to where it was so record_bug isn't a
        # surprise navigation mid-flow, and refresh the element cache — the page
        # was reloaded twice even on a same-page (at_url omitted) check.
        try:
            if s.browser._page is not None and s.browser._page.url != restore_url:
                await s.browser.goto(restore_url)
            s._last_elements = (await s.browser.get_state()).elements
        except Exception:
            pass

    if inconclusive is not None:
        return {"attempted": True, "reproduced": None, "at_url": nav_url,
                "reason": inconclusive, "target_text": target[:120], "expect": expect,
                "mocks_suspended": suspended_mocks}

    receipt = _receipt_verdict(observations, expect)
    receipt.update({
        "method": "fresh-load symptom re-check (server truth, no fixture reset)",
        "target_text": target[:120],
        "at_url": nav_url,
        "observed_present": observations,
    })
    if suspended_mocks:
        receipt["mocks_suspended"] = suspended_mocks
    return receipt


def _receipt_verdict(observations: list, expect: str) -> dict:
    """Pure verdict from a list of present/absent observations vs the claim.

    reproduced = every run matched the claim; flaky = some but not all did.
    """
    matches = [(p and expect == "present") or (not p and expect == "absent")
               for p in observations]
    hits = sum(1 for m in matches if m)
    return {
        "attempted": True,
        "reproduced": bool(matches) and all(matches),
        "flaky": 0 < hits < len(matches),
        "runs": f"{hits}/{len(matches)}",
        "expect": expect,
    }


async def _run_replay_receipt(s: "Session", verify: dict, actions: list) -> dict:
    """Reproduce a multi-step bug by REPLAYING the recorded journey from a cold
    page, then checking the symptom — stronger than re-reading one URL.

    Three-state verdict, protecting the moat:
      reproduced True  — every step re-drove AND the symptom held,
      reproduced False — steps re-drove but the symptom did NOT hold,
      reproduced None  — a step could not be re-resolved/applied (path diverged):
                         INCONCLUSIVE, never a certified reproduction.
    Runs in a fresh page (cold DOM, server re-loaded; same context so auth
    carries), so it does not disturb the agent's live page. Never raises.
    """
    expect = (verify.get("expect") or "").strip().lower()
    target = (verify.get("target_text") or verify.get("target") or "").strip()
    if expect not in ("present", "absent") or not target:
        return {"attempted": False,
                "reason": "replay receipt needs expect in {present, absent} and a non-empty target_text"}
    if s.mode != "web" or s.browser is None or s.browser._page is None:
        return {"attempted": False, "reason": "replay is only available in web mode"}
    if not actions:
        return {"attempted": False,
                "reason": "no recorded steps to replay — drive the bug via click_what/type_into/"
                          "select_into/navigate so the action trace is captured"}

    start_url = next((a.get("value") for a in actions
                      if a.get("tool") == "navigate" and a.get("value")), None)
    start_url = _resolve_url(s, start_url) if start_url else (s.url or s.browser._page.url)
    try:
        res = await s.browser.replay(start_url, actions)
    except Exception as e:
        return {"attempted": True, "mode": "replay", "reproduced": None, "diverged": True,
                "steps": len(actions), "error": str(e)[:200], "at_url": start_url}

    receipt = {
        "attempted": True, "mode": "replay", "steps": len(actions),
        "diverged": res["diverged"], "at_url": start_url, "writes_replayed": res.get("writes", 0),
        "target_text": target[:120], "expect": expect,
        "method": f"cold replay of {len(actions)} recorded step(s) in a fresh isolated context",
    }
    if res["diverged"]:
        receipt["reproduced"] = None
        receipt["step_log"] = res["steps"][-3:]
        return receipt

    # The symptom must FLIP because of the journey: hold AFTER but NOT BEFORE.
    # Visible-text only (never an input value the replay just typed). If it
    # already held at baseline (pre-existing text, localStorage residue, an
    # error/blank page where expect=absent is trivially true), we cannot
    # attribute it to the steps -> INCONCLUSIVE, never certified.
    def _holds(state):
        present = _visible_text_in_state(target, state)
        return present if expect == "present" else (not present)

    held_before = _holds(res.get("baseline_state"))
    held_after = _holds(res.get("final_state"))
    receipt["symptom_before"] = held_before
    receipt["symptom_after"] = held_after
    if held_before:
        receipt["reproduced"] = None
        receipt["reason"] = ("symptom already held before the journey — not attributable "
                             "to these steps (pre-existing / residual state)")
    else:
        receipt["reproduced"] = bool(held_after)

    # Optional minimization: narrow a confirmed reproduction to the minimal
    # sufficient steps — but ONLY for a write-free journey (re-running subsets
    # of a journey with writes would re-execute those writes many times).
    if verify.get("minimize") and receipt["reproduced"] is True and len(actions) > 1:
        if receipt.get("writes_replayed", 0) > 0:
            receipt["minimize_skipped"] = ("journey re-executes writes — minimizing would repeat "
                                           "them; skipped")
        else:
            minimal = await _minimize_replay(s, expect, target, start_url, actions)
            receipt["minimal_count"] = len(minimal)
            receipt["minimal_steps"] = [
                (f"{a.get('tool')} {a.get('description') or a.get('value') or ''}").strip()
                for a in minimal
            ]
    return receipt


async def _minimize_replay(s: "Session", expect: str, target: str, start_url: str,
                           actions: list, max_replays: int = 40) -> list:
    """Greedy 1-minimal subset of a CONFIRMED, write-free replay: drop any step
    whose removal still reproduces (flip holds, no divergence, still zero writes).
    Bounded by max_replays. Caller guarantees the full journey was write-free."""
    def _holds(state):
        present = _visible_text_in_state(target, state)
        return present if expect == "present" else (not present)

    async def _reproduces(subset: list) -> bool:
        res = await s.browser.replay(start_url, subset)
        if res["diverged"] or res.get("writes", 0) > 0:
            return False
        if _holds(res.get("baseline_state")):
            return False
        return bool(_holds(res.get("final_state")))

    current = list(actions)
    budget = max_replays
    changed = True
    while changed and budget > 0:
        changed = False
        for i in range(len(current)):
            if budget <= 0:
                break
            candidate = current[:i] + current[i + 1:]
            if not candidate:
                continue
            budget -= 1
            if await _reproduces(candidate):
                current = candidate
                changed = True
                break
    return current


async def _capture_browser_events(
    s: Session, state: PageState, console_errs: list, network_errs: list
) -> list:
    """Capture browser-side events the agent cannot see directly.

    Console messages and HTTP-layer 4xx/5xx do not surface in page state —
    they only appear via Playwright event listeners. We turn those into Bug
    records so they show up in the session report. Everything else (page
    text, counts, CSS state, toasts) is the agent's job to interpret.
    """
    recent = s.steps[s._steps_since_last_bug:]
    bugs = s.detector.process_console_errors(console_errs, state.url, recent)
    bugs.extend(s.detector.process_network_errors(network_errs, state.url, recent))
    return bugs


# Sentinel receipt for console/network events the listener captured. These are
# factual Playwright events, not agent-judged findings, and they do NOT pass
# through the reproduction receipt — so tag them rather than letting them blend
# with the independently re-confirmed findings the precision moat is about.
_AUTO_CAPTURED_RECEIPT = {
    "attempted": False,
    "auto_captured": True,
    "reason": "console/network event captured by listener — not independently re-confirmed",
}


def _file_event_bugs(s: "Session", new_bugs: list) -> None:
    """Append detector-captured event bugs to the session, tagged auto-captured."""
    for bug in new_bugs:
        if bug.reproduction_receipt is None:
            bug.reproduction_receipt = dict(_AUTO_CAPTURED_RECEIPT)
    s.bugs += new_bugs


@mcp.tool()
async def start_session(
    url: str,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> str:
    """Start a browser testing session and navigate to the given URL.

    Args:
        url: The URL to test (e.g. http://localhost:3000)
        headless: Run browser without visible window (default True)
        viewport_width: Browser viewport width in pixels
        viewport_height: Browser viewport height in pixels
    """
    global _session

    await _teardown_active_session()

    new_session = Session()
    new_session.mode = "web"
    new_session.url = url
    new_session.start_time = asyncio.get_event_loop().time()
    new_session.browser = BrowserDriver(
        headless=headless,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    try:
        await new_session.browser.start()
    except Exception as exc:
        return (
            f"start_session: failed to launch the browser — "
            f"{type(exc).__name__}: {str(exc)[:160]}\n"
            f"Run `playwright install chromium` and try again."
        )
    try:
        await new_session.browser.goto(url)
    except Exception as exc:
        # Don't leak the Playwright stack-trace; classify the common shapes.
        msg = str(exc)
        hint = ""
        if "ERR_CONNECTION_REFUSED" in msg:
            hint = " The host is up but nothing is listening on that port."
        elif "ERR_NAME_NOT_RESOLVED" in msg or "getaddrinfo" in msg:
            hint = " The hostname couldn't be resolved — check the URL."
        elif "Timeout" in msg or "timeout" in msg:
            hint = " The page took longer than 30 s to settle to networkidle."
        # Tear down the half-built browser before bubbling the error.
        try:
            await new_session.browser.stop()
        except Exception:
            pass
        return (
            f"start_session: could not load {url} — {type(exc).__name__}.{hint}\n"
            f"No session was created."
        )

    _session = new_session
    _session.pages_visited.append(url)

    state = await _session.browser.get_state()
    _session._last_elements = state.elements
    element_count = len(state.elements)

    hint = ""
    n_journaled = len(_journal_entries(urlparse(url).netloc or "default"))
    if n_journaled:
        hint = (f"\n{n_journaled} finding(s) from prior runs are journaled for this site — "
                "call regression_check() to re-test whether they're fixed or back.")

    return (
        f"Web session started.\n"
        f"Page: {state.title}\n"
        f"URL: {state.url}\n"
        f"Found {element_count} interactive elements. "
        f"Call observe() to see them.{hint}"
    )


@mcp.tool()
async def start_screen_session(target_app: str = "") -> str:
    """Start a macOS screen-mode testing session.

    Argus will probe the user's actual screen — whatever app is foreground
    (or `target_app` if given) becomes the system under test. Use this
    when the thing you're testing is not a web app, or when you want to
    test a web app through its real browser chrome rather than a headless
    Playwright instance.

    Requires Screen Recording and Accessibility grants. Run
    `argus-mcp --doctor` first if you haven't.

    Args:
        target_app: Localised app name (e.g. "Safari", "Notes", "Cursor").
                    If empty, binds to whatever is foreground at the
                    moment of the call.
    """
    global _session

    try:
        from .screen.permissions import gate_screen_mode
        from .screen.backend import ScreenBackend
        from .screen import safety as screen_safety
    except ImportError as exc:
        return (
            f"start_screen_session: screen-mode dependencies not installed.\n"
            f"  pip install argus-testing[mac]\n"
            f"  ({exc})"
        )

    missing = gate_screen_mode()
    if missing:
        lines = ["start_screen_session: missing macOS grants:"]
        for c in missing:
            lines.append(f"  - {c.name}: {c.detail}")
            lines.append(f"    Open: {c.settings_url}")
        lines.append("")
        lines.append("Run `argus-mcp --doctor` for full details, then re-try.")
        return "\n".join(lines)

    # Stale abort file from a previous session would gate every action
    # immediately — clean it up at the boundary.
    abort_path = screen_safety.abort_file_path()
    if abort_path.exists():
        try:
            abort_path.unlink()
        except OSError:
            pass

    await _teardown_active_session()

    _session = Session()
    _session.mode = "screen"
    _session.start_time = asyncio.get_event_loop().time()
    _session.screen = ScreenBackend()
    _session._safety = screen_safety.SafetyState()
    try:
        obs = await screen_safety.with_timeout(
            _session.screen.start(target_app=target_app or None)
        )
    except asyncio.TimeoutError:
        return (
            "start_screen_session: AX query timed out — the target app "
            "may be unresponsive or AX-blind. Try again, or pass an "
            "explicit `target_app` to bind to a specific known-good app."
        )
    except Exception as exc:
        return f"start_screen_session: failed to start — {exc}"

    _session._last_screen_elements = obs.elements

    # Banner to stderr so a user running argus-mcp from a terminal sees
    # the warning. Silently no-ops in MCP-over-stdio if stderr is not
    # captured by the host.
    import sys as _sys
    print(screen_safety.banner(), file=_sys.stderr)

    return (
        f"Screen session started.\n"
        f"Foreground app: {obs.foreground_app} (pid {obs.foreground_pid})\n"
        f"Window: {obs.foreground_window_title!r}\n"
        f"Screen: {obs.screen_width}x{obs.screen_height}\n"
        f"AX-tree elements: {len(obs.elements)}\n"
        f"Initial screenshot: {obs.screenshot_path}\n"
        f"\n"
        f"Safety:\n"
        f"  Session cap: {int(screen_safety.session_max_seconds())}s\n"
        f"  Per-call timeout: {screen_safety.per_call_timeout_s()}s\n"
        f"  Abort file: `touch {screen_safety.abort_file_path()}` to stop\n"
        f"\n"
        f"Call screen_observe() to see what's on screen."
    )


def _format_observation(state: PageState) -> str:
    """Render a PageState into a description-keyed observation report.

    No `[N]` indices — the agent refers to elements by what they are
    (text / role / placeholder), not by integer position. This matches
    how a human tester thinks about a screen.
    """
    lines = [f"URL: {state.url}", f"Title: {state.title}"]

    if state.page_text:
        lines.append("")
        lines.append("Page text:")
        lines.append(state.page_text[:1800])

    lines.append("")
    lines.append("Interactive elements:")
    if not state.elements:
        lines.append("  (none visible)")
    else:
        for el in state.elements:
            lines.append(f"  - {describe_element(el)}")

    if state.toast_messages:
        lines.append("")
        lines.append("Visible feedback / notifications:")
        for toast in state.toast_messages:
            lines.append(f"  [feedback] {toast}")

    if state.counts:
        lines.append("")
        lines.append("Displayed counts:")
        for label, val in state.counts.items():
            lines.append(f"  {val} {label}")

    if state.css_indicators:
        lines.append("")
        lines.append("CSS state indicators:")
        for ind in state.css_indicators:
            lines.append(f"  .{ind}")

    if state.item_lists:
        lines.append("")
        lines.append("Repeating item lists:")
        for key, items in state.item_lists.items():
            lines.append(f"  {key[:40]}: {len(items)} item(s)")
            for item in items[:3]:
                lines.append(f"    - {item[:120]}")
            if len(items) > 3:
                lines.append(f"    ... ({len(items) - 3} more)")

    if state.open_modals:
        lines.append("")
        lines.append("Open modals / dialogs / popovers:")
        for modal in state.open_modals:
            label = modal.get("ariaLabel") or modal.get("role") or "dialog"
            preview = (modal.get("text") or "").strip()[:120]
            modal_marker = " [aria-modal]" if modal.get("isModal") else ""
            lines.append(f"  - {label}{modal_marker}: {preview}")

    if state.focused:
        f = state.focused
        bits = [f.get("tag", "?")]
        for key in ("ariaLabel", "text", "placeholder", "name", "id"):
            v = f.get(key)
            if v:
                bits.append(f'{key}={v[:40]!r}')
                break
        lines.append("")
        lines.append(f"Focused element: {' '.join(bits)}")

    if state.viewport:
        v = state.viewport
        lines.append("")
        lines.append(
            f"Viewport: {v.get('innerWidth')}x{v.get('innerHeight')} | "
            f"scrollY={v.get('scrollY')} of {v.get('documentHeight')} "
            f"(at_top={v.get('atTop')}, at_bottom={v.get('atBottom')})"
        )
        if not v.get("atBottom") and v.get("documentHeight", 0) > v.get("innerHeight", 0) + 50:
            lines.append(
                "  Note: more content below the fold — scroll_down to reveal it."
            )

    lines.append("")
    lines.append(
        "To interact: click_what(\"description\"), type_into(\"field\", \"text\"), "
        "select_into(\"dropdown\", \"value\"). For deeper inspection of one element "
        "(computed styles / outerHTML / truncation), use inspect_element."
    )
    return "\n".join(lines)


def _resolve_or_error(
    s: Session,
    description: str,
    kind_filter: Optional[str] = None,
    *,
    strict_kind: bool = False,
) -> tuple[Optional[InteractiveElement], Optional[str]]:
    """Resolve a description to a single element or return an error string.

    Returns (element, None) on success, (None, error_message) on
    no_match / ambiguous / no_elements.

    Set `strict_kind=True` from tools that *cannot* gracefully handle a
    cross-kind match (e.g. type_into pointed at a link gives the user
    a Playwright stack-trace). With strict_kind, the resolver refuses
    to fall back to the full element pool.
    """
    result = resolve_element(
        description, s._last_elements,
        kind_filter=kind_filter, strict_kind=strict_kind,
    )

    if result.reason == "unique" and result.found is not None:
        return result.found, None

    if result.reason == "no_elements":
        return None, (
            "No interactive elements visible. Call observe() first, or scroll_down "
            "if you expect content below the fold."
        )

    if result.reason == "no_match":
        return None, (
            f"No element matches {description!r}. Call observe() to see what's "
            f"actually on the page, or rephrase your description."
        )

    # ambiguous
    lines = [
        f"Description {description!r} is ambiguous — multiple matches:"
    ]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_element(el)}")
    lines.append("")
    lines.append(
        "Pick a more specific description (mention surrounding text, kind hint "
        "like 'button' / 'field', or which section of the page)."
    )
    return None, "\n".join(lines)


@mcp.tool()
async def observe() -> str:
    """Observe the current target — page, app, or screen. Read this first.

    Returns the URL/window, the visible text, every interactive element
    keyed by description (no integer indices), feedback messages, counts,
    and any list-shaped repeating content. After every action, observe()
    again and reason about what changed before acting.

    The agent decides what's a bug from this output. Argus does not
    auto-flag content quality, validation behaviour, or visual issues
    here — that's your judgment to make.
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None:
        # Mode-aware (the docstring promises page/app/screen): a screen
        # session observes via the AX tree, not Playwright.
        return await (screen_observe.fn if hasattr(screen_observe, "fn") else screen_observe)()
    if s.browser._page is None:
        return ("observe: no open page — all tabs were closed. Call "
                "navigate(url) or start_session(url) to recover.")
    state = await s.browser.get_state()
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)
    return _format_observation(state)


def _format_screen_observation(obs) -> str:
    """Render a ScreenObservation into the same vibe as web-mode observe()."""
    lines = [
        f"App: {obs.foreground_app}  (pid {obs.foreground_pid})",
        f"Window: {obs.foreground_window_title!r}",
        f"Screen: {obs.screen_width}x{obs.screen_height}",
        f"Screenshot: {obs.screenshot_path or '(capture failed)'}",
        "",
        "Interactive elements (AX tree, capped):",
    ]
    if not obs.elements:
        lines.append("  (none — the app may be unresponsive or AX-blind)")
    else:
        for el in obs.elements:
            label = el.title or el.value or el.description or el.role_description or el.role
            bits = [el.role]
            if label:
                bits.append(f'"{label[:60]}"')
            if el.path and len(el.path) > 1:
                # Last 1-2 ancestors give useful disambiguating context.
                ctx = " / ".join(p for p in el.path[-2:] if p)[:60]
                if ctx:
                    bits.append(f"(in: {ctx})")
            bits.append(f"@ ({el.x},{el.y}) {el.width}x{el.height}")
            if not el.enabled:
                bits.append("[disabled]")
            if el.focused:
                bits.append("[focused]")
            lines.append(f"  - {' '.join(bits)}")
    lines.append("")
    lines.append(
        "Argus does not auto-judge content quality on screen mode either. "
        "Decide what's a bug from this output and call record_bug."
    )
    return "\n".join(lines)


def _require_screen_session() -> Session:
    """Like _require_session, but also rejects web-mode + runs safety
    precheck. Returns the session if all gates pass; raises or returns
    a string-based error message via the caller-side pattern."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        raise RuntimeError(
            "This tool is screen-mode only. Call start_screen_session() "
            "or use the equivalent web-mode tool."
        )
    return s


def _safety_or_error(s: Session) -> Optional[str]:
    """Run the screen-mode safety pre-check, returning an error string
    if the next action should be refused, or None to proceed."""
    from .screen import safety as screen_safety
    if s._safety is None:
        return "Screen-mode session has no safety state — start_screen_session() first."
    return screen_safety.precheck(s._safety)


@mcp.tool()
async def screen_observe() -> str:
    """Re-snapshot the screen — fresh screenshot + fresh AX tree of the
    foreground (or target) app.

    Same role as observe() but for screen mode. Returns the foreground
    app name, the focused window's title, the AX-tree elements with
    screen coordinates, and the path to a fresh screenshot.

    Argus does not auto-flag UX issues here. The screenshot is yours
    to look at; the AX tree is yours to reason about; call record_bug
    when you've confirmed something real.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_observe: this session is in web mode "
            f"(mode={s.mode!r}). End this session and call "
            "start_screen_session() to switch."
        )
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    try:
        obs = await screen_safety.with_timeout(s.screen.observe())
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_observe", "", "timeout", success=False,
            error="AX/observe timed out",
        )
        return (
            "screen_observe: AX query timed out. The target app may be "
            "unresponsive or AX-blind. Try `screen_observe()` again, or "
            "switch target_app via start_screen_session()."
        )
    s._last_screen_elements = obs.elements
    if obs.screenshot_path:
        s._screenshot_counter += 1
        s.screenshots.append(Screenshot(
            path=obs.screenshot_path,
            name=f"screen_{s._screenshot_counter:03d}",
            step="screen_observe",
            url=f"screen://{obs.foreground_app}",
        ))
    screen_safety.record_action(
        s._safety, "screen_observe", obs.foreground_app, "ok", success=True,
        post_screenshot=obs.screenshot_path,
    )
    return _format_screen_observation(obs)


def _resolve_screen_or_error(s, description: str, kind_filter=None, *, strict_kind: bool = False):
    """Resolve a description against the cached AX-tree elements.

    Mirrors `_resolve_or_error` for screen mode. Returns
    (element, None) on success or (None, error_message) when the
    description doesn't pin down a single element. AX-blind apps
    (Unity / Electron with custom rendering / Adobe self-render /
    web-canvas tools) get a specific hint pointing at the coordinate
    escape hatches — read the screenshot, identify (x, y), call
    screen_click_at / screen_type_at instead.
    """
    elements = s._last_screen_elements

    # AX-blind: the target app exposes nothing useful in its AX tree
    # (or the agent never observed). Surface the coordinate fallback
    # explicitly — this is the most common screen-mode failure mode
    # and the user shouldn't have to discover the workaround on their own.
    if not elements or len(elements) <= 1:
        return None, (
            "screen_click_what / screen_type_into can't help here — the "
            "target app exposes "
            f"{len(elements)} interactive AX element(s). It's effectively "
            "AX-blind (Unity, custom-rendered Electron, Adobe self-render, "
            "web-canvas tools all behave this way).\n\n"
            "Use the coordinate escape hatch:\n"
            "  1. Re-screenshot if needed (screen_observe captures a fresh PNG)\n"
            "  2. Read the screenshot yourself, identify the (x, y) of the\n"
            "     target element on the visible window\n"
            "  3. Call screen_click_at(x, y), screen_drag(...), screen_type_at(x, y, text),\n"
            "     screen_keys([...]), or screen_hover_at(x, y) as appropriate"
        )

    result = resolve_screen_element(
        description, elements,
        kind_filter=kind_filter, strict_kind=strict_kind,
    )
    if result.reason == "unique" and result.found is not None:
        return result.found, None
    if result.reason == "no_match":
        return None, (
            f"No AX element matches {description!r}. Call screen_observe() "
            f"to see what's actually exposed; the AX tree changes when the "
            f"foreground app changes or new windows open. If this app "
            f"exposes nothing useful in AX (Unity, custom Electron, etc.), "
            f"fall back to screen_click_at(x, y) with coordinates from the "
            f"screenshot."
        )
    lines = [f"Description {description!r} is ambiguous in screen mode:"]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_screen_element(el)}")
    lines.append(
        "\nPick a more specific description (use the AX role, the "
        "containing window/section, or unique substring of the label)."
    )
    return None, "\n".join(lines)


@mcp.tool()
async def screen_click_what(description: str) -> str:
    """Click a screen element matched by natural-language description.

    Resolves against the most-recent screen_observe() snapshot. Tries
    the AX press action first (atomic, no mouse hijack), falls back to
    a coordinate click via cliclick at the element's centre. After the
    click, you'll typically want to call screen_observe() to see what
    changed.

    Example: screen_click_what("Save button"),
             screen_click_what("Cancel in the Confirm dialog")
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_click_what: this session is in web mode "
            "(use click_what for web). End and start a screen session first."
        )
    err = _safety_or_error(s)
    if err:
        return err
    el, err = _resolve_screen_or_error(s, description)
    if err:
        return err

    from .screen import safety as screen_safety
    label = el.title or el.value or el.description or el.role
    s.steps.append(f'screen_click_what({description!r}) -> {el.role} {label[:40]!r}')

    pre_path = await _auto_screenshot(
        s, "screen_click_pre", f"pre-click {description!r}",
    )

    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.click(el)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_click_what", description, "timeout",
            success=False, pre_screenshot=pre_path, error="click timed out",
        )
        return f"screen_click_what({description!r}) — click timed out."

    post_path = await _auto_screenshot(
        s, "screen_click_post", f"post-click {description!r}",
    )
    screen_safety.record_action(
        s._safety, "screen_click_what", description, method,
        success=ok, pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return (
            f"screen_click_what({description!r}) — click failed via {method}.\n"
            f"The element may not implement the press action and cliclick "
            f"isn't installed (brew install cliclick), or the element moved."
        )
    return (
        f'Clicked {el.role} "{label[:60]}" via {method}.\n'
        f"  pre-screenshot: {pre_path}\n"
        f"  post-screenshot: {post_path}\n"
        f"Call screen_observe() to see what changed."
    )


@mcp.tool()
async def screen_type_into(description: str, text: str) -> str:
    """Type `text` into a screen text field matched by description.

    Tries setting AXValue directly first (cleanest for native text
    controls). If refused, focuses the element and synthesises
    keystrokes via cliclick.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_type_into: this session is in web mode "
            "(use type_into for web)."
        )
    err = _safety_or_error(s)
    if err:
        return err
    el, err = _resolve_screen_or_error(
        s, description, kind_filter="input", strict_kind=True,
    )
    if err:
        return err

    from .screen import safety as screen_safety
    label = el.title or el.value or el.description or el.role
    s.steps.append(f'screen_type_into({description!r}, ...) -> {label[:40]!r}')

    pre_path = await _auto_screenshot(s, "screen_type_pre", f"pre-type into {label[:30]}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.type_into(el, text)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_type_into", description, "timeout",
            success=False, pre_screenshot=pre_path, error="type timed out",
        )
        return f"screen_type_into({description!r}) — typing timed out."
    post_path = await _auto_screenshot(s, "screen_type_post", f"post-type into {label[:30]}")
    screen_safety.record_action(
        s._safety, "screen_type_into", description, method,
        success=ok, pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return (
            f"screen_type_into({description!r}) — typing failed via {method}.\n"
            f"The element may be read-only, or cliclick isn't installed."
        )
    return f'Typed into "{label[:60]}" via {method}.'


@mcp.tool()
async def screen_press_key(key: str) -> str:
    """Press a single key. Pass a cliclick key name: 'return', 'esc',
    'space', 'tab', 'arrow-up', 'cmd-s' (combined), etc.

    Useful for: submitting a focused form (return), dismissing a modal
    (esc), navigating menus (arrow keys), keyboard shortcuts (cmd-s).
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_press_key: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    s.steps.append(f"screen_press_key({key!r})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.press_key(key)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_press_key", key, "timeout",
            success=False, error="key press timed out",
        )
        return f"screen_press_key({key!r}) timed out."
    screen_safety.record_action(
        s._safety, "screen_press_key", key, method, success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_press_key({key!r}) failed: {method}"
    return f"Pressed {key} via {method}."


@mcp.tool()
async def screen_click_at(
    x: int,
    y: int,
    button: str = "left",
    count: int = 1,
    hold_ms: int = 0,
) -> str:
    """Click at absolute screen coordinates — the escape hatch for
    AX-blind apps (Unity, custom-rendered Electron, Adobe self-render,
    web-canvas tools). Use this when screen_click_what reports an empty
    AX tree.

    Workflow: read the most recent screenshot, identify the (x, y) of
    the element you want to click, call this tool. Re-screenshot or
    screen_observe afterwards to see what changed.

    Args:
        x, y: absolute screen coordinates (the same coordinate space
              as screen_observe's element rects).
        button: "left" (default), "right", or "middle".
        count: 1 (single, default), 2 (double-click), 3 (triple), or
               N for rapid consecutive clicks (race-condition probing).
        hold_ms: when > 0, press-and-hold for that many milliseconds
                 before releasing.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_click_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    target = f"({x},{y}) {button} x{count}" + (f" hold={hold_ms}ms" if hold_ms else "")
    s.steps.append(f"screen_click_at({target})")

    pre_path = await _auto_screenshot(s, "screen_click_at_pre", f"pre-click {target}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.click_at(x, y, button=button, count=count, hold_ms=hold_ms),
            timeout_s=max(5.0, hold_ms / 1000 + 5.0),
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_click_at", target, "timeout",
            success=False, pre_screenshot=pre_path, error="click timed out",
        )
        return f"screen_click_at({target}) timed out."
    post_path = await _auto_screenshot(s, "screen_click_at_post", f"post-click {target}")
    screen_safety.record_action(
        s._safety, "screen_click_at", target, method,
        success=ok, pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_click_at({target}) failed: {method}"
    return (
        f"Clicked at ({x},{y}) via {method}.\n"
        f"  pre:  {pre_path}\n  post: {post_path}\n"
        f"Re-observe (screen_observe) or read the post-screenshot to see what changed."
    )


@mcp.tool()
async def screen_hover_at(x: int, y: int) -> str:
    """Move the cursor to (x, y) without clicking. Use to surface
    hover-state styling on a specific element (the agent then re-
    screenshots to see the hover effect)."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_hover_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    from .screen import safety as screen_safety
    s.steps.append(f"screen_hover_at({x},{y})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.hover_at(x, y),
        )
    except asyncio.TimeoutError:
        return f"screen_hover_at timed out."
    screen_safety.record_action(
        s._safety, "screen_hover_at", f"({x},{y})", method, success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_hover_at failed: {method}"
    return f"Cursor at ({x},{y}) via {method}."


@mcp.tool()
async def screen_drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration_ms: int = 300,
) -> str:
    """Press at (from_x, from_y), move to (to_x, to_y), release.

    Required for sliders, kanban-style reordering, dragging files,
    drawing strokes, and any app that distinguishes drag from click.
    Default duration is 300 ms because some apps drop zero-duration
    drag events as accidental clicks.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_drag: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    target = f"({from_x},{from_y})->({to_x},{to_y}) {duration_ms}ms"
    s.steps.append(f"screen_drag({target})")

    pre_path = await _auto_screenshot(s, "screen_drag_pre", f"pre-drag {target}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.drag(from_x, from_y, to_x, to_y, duration_ms=duration_ms),
            timeout_s=max(5.0, duration_ms / 1000 + 5.0),
        )
    except asyncio.TimeoutError:
        return f"screen_drag({target}) timed out."
    post_path = await _auto_screenshot(s, "screen_drag_post", f"post-drag {target}")
    screen_safety.record_action(
        s._safety, "screen_drag", target, method, success=ok,
        pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_drag failed: {method}"
    return (
        f"Dragged {target} via {method}.\n"
        f"  pre:  {pre_path}\n  post: {post_path}"
    )


@mcp.tool()
async def screen_keys(keys: list) -> str:
    """Press a sequence of keys in order — the multi-key version of
    screen_press_key. Each item is a cliclick key name (`return`,
    `esc`, `space`, `tab`, `arrow-up`, …) or a combo (`cmd-s`,
    `cmd-shift-z`). Useful for keyboard-only navigation flows
    (`["arrow-down", "arrow-down", "return"]`) and OS-level shortcuts
    (`["cmd-tab"]`).
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_keys: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    if not isinstance(keys, list) or not keys:
        return "screen_keys: pass a non-empty list of cliclick key names."

    from .screen import safety as screen_safety
    s.steps.append(f"screen_keys({keys})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.press_keys(keys),
            timeout_s=max(5.0, 0.3 * len(keys) + 3.0),
        )
    except asyncio.TimeoutError:
        return f"screen_keys timed out."
    screen_safety.record_action(
        s._safety, "screen_keys", str(keys), method, success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_keys failed: {method}"
    return f"Pressed {keys} via {method}."


@mcp.tool()
async def screen_type_at(x: int, y: int, text: str) -> str:
    """Click at (x, y) to focus, then type `text`. The coordinate-
    based escape hatch for AX-blind text fields (where set-AXValue
    is unavailable)."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_type_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    from .screen import safety as screen_safety
    s.steps.append(f"screen_type_at({x},{y}, {len(text)} chars)")
    pre_path = await _auto_screenshot(s, "screen_type_at_pre", f"pre-type at ({x},{y})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.type_at(x, y, text),
            timeout_s=max(10.0, 0.05 * len(text) + 5.0),
        )
    except asyncio.TimeoutError:
        return f"screen_type_at timed out."
    post_path = await _auto_screenshot(s, "screen_type_at_post", f"post-type at ({x},{y})")
    screen_safety.record_action(
        s._safety, "screen_type_at", f"({x},{y})", method, success=ok,
        pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_type_at failed: {method}"
    return f"Typed {len(text)} chars at ({x},{y}) via {method}.\n  pre: {pre_path}\n  post: {post_path}"


@mcp.tool()
async def screen_wait_for_stable(
    timeout_s: float = 5.0,
    threshold_pct: float = 0.5,
    stable_window_ms: int = 400,
    poll_ms: int = 150,
) -> str:
    """Wait until the target window stops changing visually.

    Use this between an action and the observation that follows it.
    After clicking "New Game", the screen is in motion: loading
    spinner, scene transition, animation. Sleeping for an arbitrary
    N ms is brittle. This tool polls a screenshot every `poll_ms` and
    returns once `stable_window_ms` worth of consecutive frames stay
    below `threshold_pct` pixel difference. If `timeout_s` fires first
    you get a clear "timeout" verdict with the last frame so you can
    still inspect what the agent saw.

    Args:
        timeout_s: hard cap on wall-clock waiting.
        threshold_pct: per-frame pixel-difference threshold for "stable"
                       (0.5 means 0.5% of pixels can flicker).
        stable_window_ms: how long the page must stay below threshold
                          before we declare it settled.
        poll_ms: how often to take a fresh screenshot.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_wait_for_stable: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    target = (
        f"timeout={timeout_s}s threshold={threshold_pct}% "
        f"stable={stable_window_ms}ms poll={poll_ms}ms"
    )
    s.steps.append(f"screen_wait_for_stable({target})")

    try:
        settled, reason, final_path, stats = await screen_safety.with_timeout(
            lambda: s.screen.wait_for_stable(
                timeout_s=timeout_s,
                threshold_pct=threshold_pct,
                stable_window_ms=stable_window_ms,
                poll_ms=poll_ms,
            ),
            # Allow the wait itself to hit its own internal timeout, then
            # add a small grace before the safety wrapper trips.
            timeout_s=timeout_s + 5.0,
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_wait_for_stable", target, "outer-timeout",
            success=False, error="outer asyncio timeout fired",
        )
        return f"screen_wait_for_stable hit the outer safety timeout."

    screen_safety.record_action(
        s._safety, "screen_wait_for_stable", target,
        f"settled={settled}/{reason}", success=settled,
        post_screenshot=final_path,
        error=None if settled else reason,
    )
    if final_path:
        s._screenshot_counter += 1
        s.screenshots.append(Screenshot(
            path=final_path,
            name=f"screen_wait_{s._screenshot_counter:03d}",
            step=f"wait_for_stable: {reason}",
            url=f"screen://{s.screen._app_name or 'unknown'}",
        ))

    if settled:
        return (
            f"Settled after {stats.get('frames', '?')} frames "
            f"(last diff {stats.get('last_diff_pct', '?')}%).\n"
            f"  final: {final_path}"
        )
    if reason == "timeout":
        return (
            f"screen_wait_for_stable: timed out after {timeout_s}s, "
            f"page still moving (last diff {stats.get('last_diff_pct', '?')}%, "
            f"stable streak {stats.get('stable_for_ms', 0)}ms / "
            f"required {stable_window_ms}ms).\n"
            f"  final: {final_path}\n"
            f"  Either bump timeout_s, lower stable_window_ms, or accept that "
            f"this surface is intentionally animated."
        )
    return f"screen_wait_for_stable: {reason}.\n  final: {final_path}"


def _adhoc_screen_backend():
    """Build a one-off ScreenBackend for lifecycle calls that don't need
    an active session (launch / quit / is_running can run before a
    session is started or against a different target than the bound one)."""
    from .screen.backend import ScreenBackend
    backend = ScreenBackend()
    backend._load_frameworks()
    return backend


@mcp.tool()
async def screen_launch(target: str, wait_s: float = 8.0) -> str:
    """Launch a macOS app by localised name, bundle id, or absolute path.

    If the app is already running, returns the existing pid without
    re-launching. Otherwise shells out to `open -a <target>` and polls
    until the new process appears or `wait_s` elapses.

    This unlocks save-persistence testing: launch the app fresh, drive
    a flow, screen_quit it, screen_launch again, observe whether state
    survived.
    """
    if not target:
        return "screen_launch: target is required (app name / bundle id / path)."
    try:
        from .screen.permissions import gate_screen_mode
    except ImportError as exc:
        return f"screen_launch: screen-mode deps missing — pip install argus-testing[mac]. ({exc})"
    missing = gate_screen_mode()
    if missing:
        names = ", ".join(c.name for c in missing)
        return f"screen_launch: missing macOS grants: {names}. Run argus-mcp --doctor."

    try:
        backend = _adhoc_screen_backend()
        ok, method, pid = backend.launch(target, wait_s=wait_s)
    except Exception as exc:
        return f"screen_launch failed: {exc}"
    if not ok:
        return f"screen_launch({target!r}): {method}"

    # If a screen session is currently bound to the same app and we just
    # re-launched it, the old AX refs are stale — force the session to
    # rebind on next observe.
    s = _session
    if s.active and s.mode == "screen" and s.screen is not None:
        s.screen._app_pid = None  # forces _find_target_app to re-resolve
    return f"Launched {target!r} (pid {pid}, {method})."


@mcp.tool()
async def screen_quit(target: str, force: bool = False, wait_s: float = 8.0) -> str:
    """Quit a macOS app gracefully (like cmd-Q) or forcibly (SIGKILL).

    Use the polite path by default — apps need the chance to flush
    state if you're testing save-on-quit. Pass `force=True` only when
    the app is hung or you're explicitly testing crash recovery.
    """
    if not target:
        return "screen_quit: target is required."
    try:
        backend = _adhoc_screen_backend()
        ok, method = backend.quit(target, force=force, wait_s=wait_s)
    except Exception as exc:
        return f"screen_quit failed: {exc}"

    # If the current session was bound to the app we just killed, the
    # bound state is no longer valid — the agent should call
    # start_screen_session again.
    s = _session
    if (
        s.active and s.mode == "screen" and s.screen is not None
        and s.screen._app_name and target.lower() in s.screen._app_name.lower()
    ):
        s.screen._app_pid = None

    if not ok:
        return f"screen_quit({target!r}): {method}"
    return f"Quit {target!r} via {method}."


@mcp.tool()
async def screen_is_running(target: str) -> str:
    """Check whether a macOS app is currently running. Returns the pid
    if so. Useful as a wait/poll primitive after screen_launch /
    screen_quit, or to verify a relaunch actually replaced the old
    process."""
    if not target:
        return "screen_is_running: target is required."
    try:
        backend = _adhoc_screen_backend()
        running, pid = backend.is_running(target)
    except Exception as exc:
        return f"screen_is_running failed: {exc}"
    if running:
        return f"{target!r} is running (pid {pid})."
    return f"{target!r} is not running."


@mcp.tool()
async def screen_screenshot_region(
    x: int,
    y: int,
    width: int,
    height: int,
    name: str = "region",
) -> str:
    """Capture a rectangular region of the screen — for reading fine
    detail on a specific surface.

    Coordinates are absolute screen coords (the same space the agent
    sees in screen_observe / AX-tree element rects). VLMs are markedly
    more accurate on tight crops than full-window screenshots — when
    you need to read tiny error-toast text or distinguish two similar
    icons, capture just that region.

    Args:
        x, y: top-left corner of the region.
        width, height: pixel dimensions. Must be > 0.
        name: filename label.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_screenshot_region: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    if width <= 0 or height <= 0:
        return f"screen_screenshot_region: invalid dimensions {width}x{height}."

    s.steps.append(f"screen_screenshot_region({x},{y},{width}x{height})")
    out_dir = _output_dir() + "/screenshots"
    path = s.screen.capture_region(x, y, width, height, screenshot_dir=out_dir)
    if path is None:
        return (
            f"screen_screenshot_region: capture failed. "
            f"Check that ({x},{y}) + {width}x{height} stays inside the screen "
            f"({s.screen._app_pid and 'screen size from start_screen_session output'})."
        )
    s._screenshot_counter += 1
    s.screenshots.append(Screenshot(
        path=path,
        name=f"region_{s._screenshot_counter:03d}_{name}",
        step=f"region@({x},{y}) {width}x{height}",
        url=f"screen://{s.screen._app_name or 'unknown'}",
    ))
    return f"Region screenshot saved: {path}\n  rect: ({x},{y}) {width}x{height}"


@mcp.tool()
async def screen_session_status() -> str:
    """Show how the current screen-mode session is doing: time used vs
    session cap, action count, abort-file state, recent action trail.

    Useful between steps to verify you're not about to hit the session
    cap, and after the fact to review what Argus did.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_session_status: this session is in web mode."

    from .screen import safety as screen_safety
    import time as _time
    state = s._safety
    if state is None:
        return "screen_session_status: no safety state — bug in session bootstrap."

    elapsed = int(_time.time() - state.started_at)
    remaining = int(screen_safety.session_remaining_seconds(state))
    abort_present = screen_safety.abort_file_present()

    lines = [
        f"Screen session status:",
        f"  elapsed: {elapsed}s",
        f"  remaining (cap): {remaining}s",
        f"  action count: {state.action_count}",
        f"  aborted: {state.aborted}",
        f"  abort file present: {abort_present} ({screen_safety.abort_file_path()})",
        "",
        screen_safety.trail_summary(state),
    ]
    return "\n".join(lines)


@mcp.tool()
async def click_what(description: str) -> str:
    """Click the element best matching the natural-language `description`.

    Examples: "Login button", "Add Task", "the email field", "Delete near
    Buy groceries". Argus matches against visible text, aria-label,
    placeholder, name, id, and the parent context. Trailing kind hints
    ("button" / "field" / "link" / "dropdown") narrow the candidate pool.

    If the description is ambiguous, this returns the top candidates with
    their distinguishing properties so you can rephrase. It does not
    guess and click — that's how testers misclick.
    """
    s = _require_session()
    el, err = _resolve_or_error(s, description)
    if err:
        return err

    label = el.text or el.aria_label or el.placeholder or el.name or el.id or el.tag
    step = f'click_what({description!r}) -> "{label[:60]}"'
    s.steps.append(step)

    # Route through browser.click so a duplicate-label target hits the RESOLVED
    # element (nth-aware), not the first DOM match.
    ok = await s.browser.click(s._last_elements.index(el), s._last_elements)
    if not ok:
        return (
            f'click_what({description!r}) — failed to click "{label[:60]}".\n'
            "The element may be obscured, stale, or removed. Try observe() again."
        )
    _record_action(s, "click_what", description)

    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    if new_state.url not in s.pages_visited:
        s.pages_visited.append(new_state.url)
    return (
        f'Clicked "{label[:60]}" (via description {description!r}).\n'
        f"Now on: {new_state.url} — {len(new_state.elements)} interactive elements visible.\n"
        f"Call observe() to see what changed."
    )


@mcp.tool()
async def type_into(description: str, text: str) -> str:
    """Type `text` into the input element best matching `description`.

    Examples: type_into("email", "alice@x.com"), type_into("confirm
    password", "...") , type_into("the search box", "buy"). Resolution
    rules are the same as click_what — see that tool for ambiguity behaviour.
    """
    s = _require_session()
    el, err = _resolve_or_error(
        s, description, kind_filter="input", strict_kind=True,
    )
    if err:
        return err

    label = el.placeholder or el.name or el.aria_label or el.id or el.tag
    s.steps.append(f'type_into({description!r}, ...) -> {label[:60]}')

    ok = await s.browser.type_text(s._last_elements.index(el), text, s._last_elements)
    if not ok:
        return (
            f"type_into({description!r}) — failed. "
            f"The element may be disabled or the page may have re-rendered."
        )
    _record_action(s, "type_into", description, text)
    return f'Typed into "{label[:60]}" (via description {description!r}).'


@mcp.tool()
async def select_into(description: str, value: str) -> str:
    """Select `value` in the dropdown best matching `description`."""
    s = _require_session()
    el, err = _resolve_or_error(
        s, description, kind_filter="select", strict_kind=True,
    )
    if err:
        return err

    label = el.aria_label or el.name or el.id or el.tag
    s.steps.append(f'select_into({description!r}, {value!r}) -> {label[:60]}')

    ok = await s.browser.select_option(s._last_elements.index(el), value, s._last_elements)
    if not ok:
        return (
            f"select_into({description!r}, {value!r}) — failed. "
            f"Make sure the dropdown actually has that option "
            f"(call inspect_element to list the choices)."
        )
    _record_action(s, "select_into", description, value)
    return f'Selected "{value}" in "{label[:60]}".'


# -- richer interaction primitives --

@mcp.tool()
async def hover_what(description: str) -> str:
    """Hover the mouse over the element best matching `description`.

    Real `:hover` (not synthetic): triggers tooltips, dropdown-on-hover
    menus, hover-only action buttons. Use after the element shows up
    in observe — for divs that observe filters out (figures, plain
    `<div>`s with `:hover` rules), introspect via inspect_element or
    fall back to eval_js.
    """
    s = _require_session()
    err = _require_web_session(s, "hover_what")
    if err:
        return err
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    label = el.text or el.aria_label or el.placeholder or el.id or el.tag
    s.steps.append(f"hover_what({description!r}) -> {label[:60]}")
    idx = s._last_elements.index(el)
    ok = await s.browser.hover(idx, s._last_elements)
    if not ok:
        return f"hover_what({description!r}) — failed (element may be off-screen or detached)."
    return f"Hovered \"{label[:60]}\". observe() to see what just appeared."


@mcp.tool()
async def right_click(description: str) -> str:
    """Right-click (button=right) the element best matching `description`.

    Use for custom context menus, "Open in new tab" tests, draggable
    handles that respond to right-click. Any context menu or alert that
    appears as a result is handled by the dialog queue (see
    set_dialog_handler).
    """
    s = _require_session()
    err = _require_web_session(s, "right_click")
    if err:
        return err
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    label = el.text or el.aria_label or el.placeholder or el.id or el.tag
    s.steps.append(f"right_click({description!r}) -> {label[:60]}")
    idx = s._last_elements.index(el)
    ok = await s.browser.right_click(idx, s._last_elements)
    if not ok:
        return f"right_click({description!r}) — failed."
    return f"Right-clicked \"{label[:60]}\". observe() to see the menu / new state."


@mcp.tool()
async def drag_what(from_description: str, to_description: str) -> str:
    """Drag the element matching `from_description` onto `to_description`.

    Wraps Playwright's `page.drag_and_drop` (real mouse events — works
    for both HTML5 DnD and Sortable.js / dnd-kit / react-beautiful-dnd
    style mousedown+mousemove). Both endpoints must be visible in
    observe(); for `[draggable="true"]` divs that's automatic, for
    other element types the agent may need to scroll first.
    """
    s = _require_session()
    err = _require_web_session(s, "drag_what")
    if err:
        return err
    src, err = _resolve_or_error(s, from_description)
    if err:
        return f"drag_what(from): {err}"
    tgt, err = _resolve_or_error(s, to_description)
    if err:
        return f"drag_what(to): {err}"
    src_label = src.text or src.id or src.tag
    tgt_label = tgt.text or tgt.id or tgt.tag
    s.steps.append(f"drag_what({from_description!r} -> {to_description!r})")
    src_idx = s._last_elements.index(src)
    tgt_idx = s._last_elements.index(tgt)
    ok = await s.browser.drag(src_idx, tgt_idx, s._last_elements)
    if not ok:
        return (
            f"drag_what({from_description!r} -> {to_description!r}) — failed. "
            f"The source may not actually be draggable, or the target may be "
            f"off-screen."
        )
    return f'Dragged "{src_label[:40]}" onto "{tgt_label[:40]}". observe() to verify.'


@mcp.tool()
async def upload_file(description: str, paths: list) -> str:
    """Attach one or more local files to the file `<input>` matching
    `description`.

    Wraps Playwright's `set_input_files` — works on both visible and
    hidden file inputs (most modern UIs hide the real input behind a
    styled label). For drag-drop upload zones that don't have an
    underlying `<input type=file>`, this won't work; those need a
    real drag.

    Args:
        description: Match the file input. "file", "upload", or the
                     visible label text.
        paths: List of absolute paths to files to attach. Single file:
               pass a one-element list.
    """
    s = _require_session()
    err = _require_web_session(s, "upload_file")
    if err:
        return err
    if not paths:
        return "upload_file: paths is empty."
    el, err = _resolve_or_error(s, description, kind_filter="input")
    if err:
        return err
    if (el.type or "").lower() != "file":
        return (
            f"upload_file: matched element is `<input type={el.type!r}>`, "
            f"not a file input. Pass a description that matches the file "
            f"input specifically."
        )
    s.steps.append(f"upload_file({description!r}, {len(paths)} file(s))")
    idx = s._last_elements.index(el)
    ok = await s.browser.upload_file(idx, paths, s._last_elements)
    if not ok:
        return f"upload_file({description!r}) — failed."
    return f"Attached {len(paths)} file(s) to {description!r}: {', '.join(paths)}"


@mcp.tool()
async def set_dialog_handler(action: str = "accept", text: str = "") -> str:
    """Queue a response for the next JS `alert` / `confirm` / `prompt`.

    Playwright's default is to auto-dismiss every dialog, which means
    `confirm()` always sees Cancel and `prompt()` always sees null.
    Call this BEFORE the click that triggers the dialog so the agent
    actually controls OK vs Cancel and the prompt input value.

    Args:
        action: "accept" (OK) or "dismiss" (Cancel).
        text: For prompt(), the text to type before accepting. Ignored
              for alert/confirm.
    """
    s = _require_session()
    err = _require_web_session(s, "set_dialog_handler")
    if err:
        return err
    if action not in ("accept", "dismiss"):
        return "set_dialog_handler: action must be 'accept' or 'dismiss'."
    s.browser.queue_dialog_response(action, text)
    s.steps.append(f"set_dialog_handler({action!r}, text={text!r})")
    return (
        f"Next dialog will be {action}ed"
        + (f" with text {text!r}" if text else "")
        + ". Trigger the action that fires the dialog now."
    )


@mcp.tool()
async def inspect_element(description: str) -> str:
    """Get computed styles, ARIA metadata, and outerHTML for one element.

    Use this when you suspect a visual / a11y / truncation bug on a
    specific surface and observe()'s summary doesn't tell you enough.
    Returns:
      - rendered styles (color, background, font-size/weight, display,
        visibility, opacity, position, z-index, overflow, cursor, etc.)
      - bounding rect + whether it's in the viewport
      - whether the element is visually truncated by CSS (scrollWidth >
        clientWidth with overflow: hidden / text-overflow: ellipsis)
      - aria-label / aria-describedby / aria-hidden / role / title
      - associated <label> text(s)
      - disabled / readonly / focused state
      - first 1.5 KB of outerHTML

    Argus does not auto-judge anything from this output. You read it
    and decide whether anything you see warrants record_bug.
    """
    s = _require_session()
    el, err = _resolve_or_error(s, description)
    if err:
        return err

    selector = s.browser._build_selector(el)
    info = await s.browser.inspect_element(selector)
    if not info.get("found"):
        return (
            f"inspect_element({description!r}) — could not re-locate element "
            f"via selector {selector!r}. The DOM may have changed; observe() again."
        )

    s.steps.append(f'inspect_element({description!r})')

    lines = [f"Inspecting {description!r} (resolved to <{info['tag']}>)"]
    lines.append("")
    lines.append("Visible text: " + (info.get("text") or "<none>")[:160])
    rect = info.get("rect", {})
    lines.append(
        f"Rect: x={rect.get('x', 0):.0f} y={rect.get('y', 0):.0f} "
        f"w={rect.get('width', 0):.0f} h={rect.get('height', 0):.0f} "
        f"in_viewport={rect.get('inViewport')}"
    )
    if info.get("truncated"):
        sd = info.get("scrollDimensions", {})
        lines.append(
            f"  TRUNCATED: scrollWidth={sd.get('scrollWidth')} > clientWidth={sd.get('clientWidth')} "
            f"(or scrollHeight > clientHeight) with overflow hidden — text is silently cut off."
        )
    if info.get("focused"):
        lines.append("Focus: this element currently has focus.")

    lines.append("")
    lines.append("Computed styles:")
    for k, v in (info.get("styles") or {}).items():
        lines.append(f"  {k}: {v}")

    lines.append("")
    lines.append("Accessibility:")
    lines.append(f"  role: {info.get('role') or '(default)'}")
    lines.append(f"  aria-label: {info.get('ariaLabel') or '(none)'}")
    lines.append(f"  aria-describedby: {info.get('ariaDescribedby') or '(none)'}")
    lines.append(f"  aria-hidden: {info.get('ariaHidden') or '(false/unset)'}")
    lines.append(f"  title: {info.get('title') or '(none)'}")
    lines.append(f"  disabled: {info.get('disabled')}, readonly: {info.get('readonly')}")
    if info.get("labels"):
        lines.append(f"  associated <label>(s): {info['labels']}")

    lines.append("")
    lines.append("outerHTML (first 1500 chars):")
    lines.append(info.get("outerHtml") or "(unavailable)")
    return "\n".join(lines)


@mcp.tool()
async def eval_js(code: str) -> str:
    """Run arbitrary JavaScript in the page context and return the result.

    Disabled by default because it can read cookies, mutate state, and
    issue any fetch the page is allowed to. Enable with the `--unsafe`
    flag at server start (or `ARGUS_UNSAFE_EVAL=1`). Use this when:
      - inspecting state the standard tools don't expose (window.X,
        a global config object, IndexedDB contents);
      - resetting or seeding a test fixture via its own internal
        endpoints (`fetch('/api/test/reset', {method: 'POST'})`);
      - probing edge cases that require triggering JS the UI doesn't
        expose (race conditions, double-submit by direct fetch).

    The tool returns the JSON-serialised return value of the JS
    expression. If your code returns an unserialisable object, wrap
    it (e.g. `JSON.stringify(...)` or pluck specific fields).

    Argus does not auto-record bugs from eval_js output. If you find
    something via eval_js, call record_bug like with anything else.

    Args:
        code: A JS expression or arrow function. Examples:
            "() => window.location.href"
            "() => fetch('/api/test/reset', {method:'POST'}).then(r=>r.status)"
            "() => Object.keys(window.appConfig || {})"
    """
    if os.environ.get("ARGUS_UNSAFE_EVAL") != "1":
        return (
            "eval_js is disabled. Restart argus-mcp with the --unsafe flag "
            "(or set ARGUS_UNSAFE_EVAL=1) to enable arbitrary JS execution. "
            "It is off by default because the JS you run has the same "
            "powers as the page itself."
        )

    s = _require_session()
    s.steps.append(f"eval_js: {code[:80]}")

    try:
        result = await s.browser._page.evaluate(code)
    except Exception as exc:
        return f"eval_js failed: {exc}"

    # Serialise. Playwright already gives us Python primitives for JSON-able
    # results. For other shapes, fall back to repr.
    try:
        import json as _json
        rendered = _json.dumps(result, default=str)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "... [truncated]"
        return f"eval_js result: {rendered}"
    except Exception:
        return f"eval_js result (repr): {result!r}"


# ── network inspection + mocking ────────────────────────────────────


def _filter_network_log(log, url_substring=None, method=None, status_min=None):
    """Slice the network log by simple criteria — used by network_requests
    and network_request to surface what the agent cares about without
    paging through everything."""
    out = log
    if url_substring:
        out = [e for e in out if url_substring in (e.get("url") or "")]
    if method:
        m = method.upper()
        out = [e for e in out if (e.get("method") or "").upper() == m]
    if status_min is not None:
        out = [e for e in out if (e.get("status") or 0) >= status_min]
    return out


@mcp.tool()
async def network_requests(
    url_substring: str = "",
    method: str = "",
    status_min: int = 0,
    limit: int = 30,
) -> str:
    """List the HTTP requests this page has issued, newest last.

    Every request and response is captured in the background; this tool
    drains the current snapshot, optionally filtered. Use it to verify
    "did the right /api/foo get called", "what method", "what response
    status", and to spot requests the UI doesn't surface (analytics,
    background polls, third-party widgets).

    Args:
        url_substring: keep only requests whose URL contains this string.
        method: keep only this method (GET / POST / PUT / …). Empty = all.
        status_min: keep only responses ≥ this status (e.g. 400 to find
                    failures only). 0 = all.
        limit: cap the returned list (default 30).
    """
    s = _require_session()
    err = _require_web_session(s, "network_requests")
    if err:
        return err

    log = s.browser.network_log_snapshot()
    filtered = _filter_network_log(log, url_substring or None, method or None,
                                   status_min if status_min > 0 else None)
    shown = filtered[-limit:]

    if not shown:
        return (
            f"No matching requests "
            f"(filters: url~{url_substring!r}, method={method!r}, "
            f"status≥{status_min}). Total captured this session: {len(log)}."
        )

    lines = [
        f"Network log: {len(shown)} of {len(filtered)} matching "
        f"({len(log)} total this session)."
    ]
    for e in shown:
        url_display = _redact(e.get("url") or "")[:90]
        status = e.get("status")
        status_s = str(status) if status is not None else "pending"
        size = e.get("response_size")
        size_s = f" {size}B" if size is not None else ""
        lines.append(
            f"  [{status_s:>7}] {(e.get('method') or '?'):<6} "
            f"{e.get('resource_type', ''):<10} {url_display}{size_s}"
        )
    return "\n".join(lines)


@mcp.tool()
async def network_request(
    url_substring: str,
    method: str = "",
) -> str:
    """Get the full request/response detail for ONE captured request.

    Picks the most recent request whose URL contains `url_substring`
    (and matches `method` if given). Returns headers, post-body,
    response status, response headers, response size — everything you
    need to assert "this exact payload was sent and returned 200".

    For the list view, use network_requests.
    """
    s = _require_session()
    err = _require_web_session(s, "network_request")
    if err:
        return err
    if not url_substring:
        return "network_request: pass a url_substring to identify the request."

    log = s.browser.network_log_snapshot()
    matches = _filter_network_log(log, url_substring, method or None)
    if not matches:
        return (
            f"No request matches url~{url_substring!r}"
            f"{', method=' + method if method else ''}. "
            f"Total captured: {len(log)}."
        )

    e = matches[-1]
    import json as _json
    lines = [
        f"Request: {e.get('method')} {_redact(e.get('url') or '')}",
        f"  resource_type: {e.get('resource_type')}",
        f"  page_url:      {e.get('page_url')}",
        f"  started:       {e.get('started_at')}",
        "",
        "Request headers:",
    ]
    for k, v in _redact_headers(e.get("headers") or {}).items():
        lines.append(f"  {k}: {v}")
    if e.get("post_data"):
        # The request body of a login POST is the plaintext password — redact.
        body = _redact(e["post_data"])
        lines.append("")
        lines.append("Request body:")
        lines.append("  " + (body[:1500] + ("…[truncated]" if len(body) > 1500 else "")))

    lines.append("")
    status = e.get("status")
    if status is None:
        lines.append("Response: (still pending — request fired but no response yet)")
    else:
        lines.append(f"Response: HTTP {status}")
        size = e.get("response_size")
        if size is not None:
            lines.append(f"  body size: {size} bytes")
        if e.get("finished_at"):
            lines.append(f"  finished:  {e['finished_at']}")
        rh = _redact_headers(e.get("response_headers") or {})
        if rh:
            lines.append("  Response headers:")
            for k, v in rh.items():
                lines.append(f"    {k}: {v}")
        rbody = e.get("response_body")
        if rbody:
            lines.append("")
            lines.append("Response body:")
            # The whole point: read what the server actually returned — a 200
            # whose body says {"error": ...} is the deception behind a toast.
            lines.append(rbody)

    return "\n".join(lines)


@mcp.tool()
async def network_mock(
    url_pattern: str,
    status: int = 200,
    body: str = "",
    content_type: str = "application/json",
) -> str:
    """Intercept any request matching `url_pattern` and return a canned
    response, instead of hitting the network.

    Use this to test how the UI handles specific server responses
    (5xx, 401, slow JSON, malformed payload) without needing the
    backend to cooperate.

    Args:
        url_pattern: glob-style (`**/api/users`) or full URL substring.
                     Playwright's page.route() patterns apply.
        status: HTTP status code to return (default 200).
        body: response body string.
        content_type: response Content-Type header.
    """
    s = _require_session()
    err = _require_web_session(s, "network_mock")
    if err:
        return err
    if not url_pattern:
        return "network_mock: pass a url_pattern."
    try:
        await s.browser.add_route(
            pattern=url_pattern, status=status, body=body,
            content_type=content_type,
        )
    except Exception as exc:
        return f"network_mock failed: {type(exc).__name__}: {exc}"
    s.steps.append(f"network_mock({url_pattern!r}) -> HTTP {status}")
    return (
        f"Mock registered: {url_pattern} -> HTTP {status} "
        f"({content_type}, {len(body)}B body).\n"
        f"Subsequent matching requests will return this canned response.\n"
        f"Call network_unmock or network_clear_mocks to remove."
    )


@mcp.tool()
async def network_unmock(url_pattern: str) -> str:
    """Drop a mock previously registered via network_mock."""
    s = _require_session()
    err = _require_web_session(s, "network_unmock")
    if err:
        return err
    try:
        removed = await s.browser.remove_route(url_pattern)
    except Exception as exc:
        return f"network_unmock failed: {exc}"
    if removed:
        return f"Unmocked: {url_pattern}"
    return f"No mock registered for {url_pattern!r}."


@mcp.tool()
async def network_clear_mocks() -> str:
    """Drop every mock registered this session."""
    s = _require_session()
    err = _require_web_session(s, "network_clear_mocks")
    if err:
        return err
    n = await s.browser.clear_routes()
    return f"Cleared {n} mock(s)."


@mcp.tool()
async def network_clear_log() -> str:
    """Drop the captured request/response log (the mocks themselves stay
    registered). Useful between scenarios to isolate the call set."""
    s = _require_session()
    err = _require_web_session(s, "network_clear_log")
    if err:
        return err
    n = s.browser.clear_network_log()
    return f"Cleared {n} captured request entries."


# -- cookies + storage state --

@mcp.tool()
async def cookies_get(url: str = "") -> str:
    """List cookies on the current browser context.

    Use this to verify what the server / front-end set after login,
    consent banner, A/B opt-in, etc. Pair with cookies_set to seed
    a known-good auth session and skip the login UI for downstream
    tests.

    Args:
        url: Filter to cookies that would be sent for this URL.
             Empty = return every cookie on the context.
    """
    s = _require_session()
    err = _require_web_session(s, "cookies_get")
    if err:
        return err
    cookies = await s.browser.cookies_get(url or None)
    if not cookies:
        return "No cookies." + (f" (filter: url={url})" if url else "")
    lines = [f"{len(cookies)} cookie(s){' for ' + url if url else ''}:"]
    for c in cookies:
        domain = c.get("domain", "")
        path = c.get("path", "/")
        name = c.get("name", "")
        val = c.get("value", "")
        if len(val) > 60:
            val = val[:57] + "..."
        flags = []
        if c.get("httpOnly"):
            flags.append("HttpOnly")
        if c.get("secure"):
            flags.append("Secure")
        if c.get("sameSite"):
            flags.append(f"SameSite={c['sameSite']}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {name}={val}  ({domain}{path}){flag_str}")
    return "\n".join(lines)


@mcp.tool()
async def cookies_set(cookies: list) -> str:
    """Inject one or more cookies into the current browser context.

    Each cookie must be a dict with at least name + value, and either
    a `url` field OR both `domain` + `path`. Common extras: expires
    (unix seconds), httpOnly, secure, sameSite ("Strict"|"Lax"|"None").

    Useful for skipping the login UI: paste a session cookie captured
    out-of-band and the next navigate() request lands authenticated.

    Args:
        cookies: List of cookie dicts. Example:
                 [{"name": "session", "value": "abc",
                   "url": "http://127.0.0.1:5555"}]
    """
    s = _require_session()
    err = _require_web_session(s, "cookies_set")
    if err:
        return err
    if not cookies:
        return "cookies_set: pass a non-empty list of cookie dicts."
    n = await s.browser.cookies_set(cookies)
    s.steps.append(f"cookies_set({n} cookie(s))")
    if n == 0:
        return ("cookies_set: 0 set — check that each entry has name + value "
                "and either url OR domain+path.")
    return f"Set {n} cookie(s) on the context."


@mcp.tool()
async def cookies_clear() -> str:
    """Clear every cookie on the browser context (logout-all-the-things)."""
    s = _require_session()
    err = _require_web_session(s, "cookies_clear")
    if err:
        return err
    ok = await s.browser.cookies_clear()
    s.steps.append("cookies_clear")
    return "Cleared all cookies." if ok else "cookies_clear: failed."


@mcp.tool()
async def storage_get(kind: str = "local") -> str:
    """Read every key/value in localStorage or sessionStorage of the
    current page.

    Args:
        kind: "local" (default) for localStorage, "session" for
              sessionStorage.
    """
    s = _require_session()
    err = _require_web_session(s, "storage_get")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_get: kind must be 'local' or 'session'."
    items = await s.browser.storage_get(kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    if not items:
        return f"{label}: empty."
    lines = [f"{label} ({len(items)} key(s)):"]
    for k, v in items.items():
        sval = v if v is not None else ""
        if len(sval) > 200:
            sval = sval[:197] + "..."
        lines.append(f"  {k} = {sval}")
    return "\n".join(lines)


@mcp.tool()
async def storage_set(key: str, value: str, kind: str = "local") -> str:
    """Write a single key/value to localStorage or sessionStorage.

    Use this to seed a feature-flag, theme preference, or onboarding-
    completed marker without driving the UI to set it.

    Args:
        key: Storage key.
        value: Value (always stored as a string in browser storage).
        kind: "local" or "session" (default "local").
    """
    s = _require_session()
    err = _require_web_session(s, "storage_set")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_set: kind must be 'local' or 'session'."
    ok = await s.browser.storage_set(key, value, kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_set({label}, {key!r})")
    return f"Set {label}[{key!r}]." if ok else f"storage_set: failed for {key!r}."


@mcp.tool()
async def storage_remove(key: str, kind: str = "local") -> str:
    """Delete a single key from localStorage or sessionStorage."""
    s = _require_session()
    err = _require_web_session(s, "storage_remove")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_remove: kind must be 'local' or 'session'."
    ok = await s.browser.storage_remove(key, kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_remove({label}, {key!r})")
    return f"Removed {label}[{key!r}]." if ok else f"storage_remove: failed."


@mcp.tool()
async def storage_clear(kind: str = "local") -> str:
    """Clear all keys from localStorage or sessionStorage on this page."""
    s = _require_session()
    err = _require_web_session(s, "storage_clear")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_clear: kind must be 'local' or 'session'."
    ok = await s.browser.storage_clear(kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_clear({label})")
    return f"Cleared {label}." if ok else "storage_clear: failed."


# -- state capsules --

def _capsule_dir() -> Path:
    return Path(_output_dir()) / ".argus" / "capsules"


def _capsule_path(origin: str, name: str) -> Path:
    safe_origin = re.sub(r"[^A-Za-z0-9._-]", "_", origin or "default")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "default")
    # Capsules hold raw auth cookies. They live under .argus/ (gitignored), and
    # capsule_save also drops a defensive `.gitignore: *` so they can never be
    # committed even if ARGUS_OUTPUT_DIR points outside the default tree.
    return _capsule_dir() / safe_origin / f"{safe_name}.json"


@mcp.tool()
async def capsule_save(name: str, liveness_marker: str = "") -> str:
    """Snapshot the current logged-in / seeded state as a named, restorable capsule.

    Captures cookies + localStorage + sessionStorage + the current URL. Mint the
    state through the REAL UI first (sign up, create data) and THEN save — that
    keeps it honest (if onboarding is broken, you felt it). Pass a
    `liveness_marker`: text visible ONLY while the capsule is still valid (e.g.
    the logged-in user's name) so a later restore can tell live from stale.

    Args:
        name: Capsule name to save under (per-origin).
        liveness_marker: Text that proves the restored state is still valid.
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or s.browser._page is None:
        return "capsule_save: this tool is web-mode only."
    capsule = await s.browser.capsule_capture()
    capsule["liveness_marker"] = liveness_marker.strip()
    origin = urlparse(capsule.get("url") or "").netloc or "default"
    path = _capsule_path(origin, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Defensive: capsules carry live auth cookies — never let them be
        # committed, even under a non-default ARGUS_OUTPUT_DIR.
        gi = _capsule_dir() / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
        import json as _json
        path.write_text(_json.dumps(capsule, indent=2))
    except Exception as exc:
        return f"capsule_save: could not write capsule — {type(exc).__name__}: {str(exc)[:120]}"
    s.steps.append(f"capsule_save({name!r})")
    n_c = len(capsule.get("cookies") or [])
    n_l = len(capsule.get("local") or {})
    n_s = len(capsule.get("session") or {})
    tail = (f" Liveness marker: {liveness_marker.strip()!r}." if liveness_marker.strip()
            else " No liveness marker set — restore can't verify validity (recommend setting one).")
    return (f"Saved capsule {name!r} for {origin} "
            f"({n_c} cookies, {n_l} localStorage, {n_s} sessionStorage).{tail}")


@mcp.tool()
async def capsule_restore(name: str) -> str:
    """Restore a saved capsule onto this session and verify it is still live.

    Sets the cookies + storage, navigates to the captured URL, then checks the
    saved liveness marker. Returns whether the restored state is LIVE or STALE.
    A STALE capsule (the server session expired) cannot be trusted — any bug you
    record afterwards is flagged unreliable until you re-mint the state.

    Args:
        name: Capsule name to restore (looked up for the current origin).
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or s.browser._page is None:
        return "capsule_restore: this tool is web-mode only."
    # Look up by the current origin first, but fall back to scanning every saved
    # origin for this name — so restore works from about:blank or after the app
    # moved to a different port/host alias (the whole point is to skip re-login).
    origin = urlparse(s.browser._page.url).netloc or "default"
    path = _capsule_path(origin, name)
    if not path.exists():
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "default")
        matches = list((Path(_output_dir()) / ".argus" / "capsules").glob(f"*/{safe_name}.json"))
        if len(matches) == 1:
            path = matches[0]
        elif len(matches) > 1:
            origins = ", ".join(p.parent.name for p in matches)
            return (f"capsule_restore: {name!r} exists for multiple origins ({origins}); "
                    f"navigate to the target origin first to disambiguate.")
        else:
            return (f"capsule_restore: no capsule named {name!r}. "
                    f"capsule_save({name!r}, liveness_marker=...) it first.")
    try:
        import json as _json
        capsule = _json.loads(path.read_text())
        applied = await s.browser.capsule_apply(capsule)
    except Exception as exc:
        return f"capsule_restore: failed to apply — {type(exc).__name__}: {str(exc)[:120]}"
    state = await s.browser.get_state()
    s._last_elements = state.elements
    s.steps.append(f"capsule_restore({name!r})")

    warn = ""
    if not applied.get("origin_ok", True):
        warn = (" WARNING: navigating to the capsule URL redirected cross-origin "
                "(likely an expired session bouncing to login) — storage was NOT applied.")
    elif applied.get("cookies_expected", 0) and applied.get("cookies", 0) == 0:
        warn = " WARNING: zero cookies were applied (injection failed) — you are likely NOT authenticated."

    marker = (capsule.get("liveness_marker") or "").strip()
    if not marker:
        s._capsule_marker = None
        return (f"Restored capsule {name!r} "
                f"({applied.get('cookies', 0)} cookies, {applied.get('local', 0)} localStorage, "
                f"{applied.get('session', 0)} sessionStorage), but no liveness marker was saved — "
                f"validity is unverified; observe() to confirm.{warn}")
    s._capsule_marker = marker
    if not _marker_visible(marker, state):
        # SPAs render identity after networkidle (a /me fetch + hydration) —
        # give the marker a beat to appear before declaring the session dead.
        await s.browser.wait_for_text(marker, timeout_s=5)
        state = await s.browser.get_state()
        s._last_elements = state.elements
    if _marker_visible(marker, state):
        return (f"Restored capsule {name!r} — appears LIVE (marker {marker!r} is visible; "
                f"confirm it's genuinely auth-gated).{warn}")
    return (f"Restored capsule {name!r} — STALE: liveness marker {marker!r} is not visible. "
            f"The server session has likely expired; re-mint the state through the UI. "
            f"Bugs recorded while it stays absent are flagged unreliable.{warn}")


# -- multi-tab + waits --

@mcp.tool()
async def tabs_list() -> str:
    """List every open tab/popup in the current browser context.

    Many real flows spawn a second tab — OAuth, "open in new tab",
    target=_blank links, window.open from ad code. Without this, the
    agent only ever sees the original page and misses bugs in the
    spawned context.
    """
    s = _require_session()
    err = _require_web_session(s, "tabs_list")
    if err:
        return err
    tabs = await s.browser.tabs_list()
    if not tabs:
        return "No tabs."
    lines = [f"{len(tabs)} tab(s):"]
    for t in tabs:
        marker = "* " if t["active"] else "  "
        title = (t["title"] or "")[:60]
        lines.append(f"{marker}[{t['index']}] {title} — {t['url']}")
    lines.append("(* = active. Use tabs_switch(index) to focus another tab.)")
    return "\n".join(lines)


@mcp.tool()
async def tabs_switch(index: int) -> str:
    """Make the tab at the given index the active tab. All subsequent
    observe / click / type / network / storage calls target it."""
    s = _require_session()
    err = _require_web_session(s, "tabs_switch")
    if err:
        return err
    ok = await s.browser.tabs_switch(index)
    if not ok:
        return f"tabs_switch: no tab at index {index}. Call tabs_list to see available tabs."
    # The active page changed — refresh the resolver pool so the next
    # click_what/type_into resolves against THIS tab, not the previous one.
    s._last_elements = (await s.browser.get_state()).elements
    s.steps.append(f"tabs_switch({index})")
    return f"Switched to tab {index}: {s.browser._page.url}. observe() to see its elements."


@mcp.tool()
async def tabs_close(index: int) -> str:
    """Close the tab at the given index. If it was active, focus
    falls back to the first remaining tab."""
    s = _require_session()
    err = _require_web_session(s, "tabs_close")
    if err:
        return err
    ok = await s.browser.tabs_close(index)
    if not ok:
        return f"tabs_close: no tab at index {index}."
    s.steps.append(f"tabs_close({index})")
    if s.browser._page is None:
        return ("Closed the last tab — no open page remains. Call "
                "navigate(url) or start_session(url) to continue.")
    # Focus may have moved to another tab — refresh the resolver pool.
    s._last_elements = (await s.browser.get_state()).elements
    return f"Closed tab {index}. Active page is now {s.browser._page.url}."


@mcp.tool()
async def wait_for_text(text: str, timeout_s: float = 10.0) -> str:
    """Block until `text` appears anywhere in the page body, or timeout.

    Use this after triggering an async action when you know what the
    success/failure copy will say ("Saved", "Invalid email"). Cheaper
    and more deterministic than polling observe() in a loop.

    Args:
        text: Substring to look for in document.body.innerText.
        timeout_s: Seconds to wait before giving up (default 10).
    """
    s = _require_session()
    err = _require_web_session(s, "wait_for_text")
    if err:
        return err
    if not text:
        return "wait_for_text: pass a non-empty text."
    found = await s.browser.wait_for_text(text, timeout_s)
    s.steps.append(f"wait_for_text({text!r}, {timeout_s}s) -> {'found' if found else 'timeout'}")
    if found:
        return f"Found: {text!r}"
    return f"Timed out after {timeout_s}s — {text!r} did not appear."


@mcp.tool()
async def wait_for_request(
    url_substring: str,
    method: str = "",
    timeout_s: float = 10.0,
) -> str:
    """Block until the next outgoing request matches the filter, or
    timeout.

    Pattern: trigger an action, then wait for the API call it should
    fire — confirms the front-end actually wired the click to the
    network. Combine with network_request(url_substring=...) to read
    the full payload after.

    Args:
        url_substring: Substring that must appear in the request URL.
        method: Optional HTTP method filter (GET/POST/PUT/DELETE).
        timeout_s: Seconds to wait (default 10).
    """
    s = _require_session()
    err = _require_web_session(s, "wait_for_request")
    if err:
        return err
    if not url_substring:
        return "wait_for_request: pass a url_substring."
    snap = await s.browser.wait_for_request(
        url_substring, method or None, timeout_s,
    )
    label = f"{method or 'ANY'} ~{url_substring!r}"
    s.steps.append(
        f"wait_for_request({label}, {timeout_s}s) -> {'matched' if snap else 'timeout'}"
    )
    if snap:
        post = snap.get("post_data") or ""
        if post and len(post) > 200:
            post = post[:197] + "..."
        return (
            f"Matched: {snap['method']} {snap['url']}\n"
            f"  resource_type: {snap['resource_type']}\n"
            f"  post_data: {post or '(none)'}"
        )
    return f"Timed out after {timeout_s}s — no request matched {label}."


@mcp.tool()
async def navigate(url: str) -> str:
    """Navigate to a specific URL.

    Args:
        url: The URL to navigate to
    """
    s = _require_session()
    err = _require_web_session(s, "navigate")
    if err:
        return err
    step = f"Navigate to {url}"
    s.steps.append(step)

    await s.browser.goto(url)
    state = await s.browser.get_state()
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)
    _record_action(s, "navigate", value=url)

    return f"Navigated to {state.url} — {state.title} ({len(state.elements)} elements)"


@mcp.tool()
async def go_back() -> str:
    """Go back to the previous page."""
    s = _require_session()
    err = _require_web_session(s, "go_back")
    if err:
        return err
    s.steps.append("Go back")

    ok = await s.browser.go_back()
    if ok:
        state = await s.browser.get_state()
        s._last_elements = state.elements
        return f"Went back to {state.url}"
    return "Failed to go back"


@mcp.tool()
async def scroll_down() -> str:
    """Scroll the page down to reveal more content."""
    s = _require_session()
    err = _require_web_session(s, "scroll_down")
    if err:
        return err
    s.steps.append("Scroll down")
    await s.browser.scroll_down()
    return "Scrolled down. Call observe() to see what's now in view."


@mcp.tool()
async def screenshot(
    name: str = "screenshot",
    element: str = "",
    full_page: bool = False,
) -> str:
    """Capture a screenshot — full viewport, full page, or one element.

    Use this whenever something looks visually off and you want evidence
    for a record_bug call, or when you want a before/after pair to feed
    into screenshot_diff.

    Args:
        name: Filename label (no extension).
        element: Optional element description (same syntax as click_what).
                 If given, crops the screenshot to that element's bounds.
                 Use for visual hierarchy / truncation / contrast checks.
        full_page: If True, capture the entire scrollable page rather
                   than just the viewport. Ignored when `element` is set.
    """
    s = _require_session()
    last_step = s.steps[-1] if s.steps else "Initial state"

    if element:
        el, err = _resolve_or_error(s, element)
        if err:
            return err
        s._screenshot_counter += 1
        safe_name = f"{s._screenshot_counter:03d}_elem_{name}"
        path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")
        selector = s.browser._build_selector(el)
        result = await s.browser.element_screenshot(selector, path)
        if result is None:
            return (
                f"screenshot(element={element!r}) — could not capture; the "
                f"element may have moved or detached. observe() and try again."
            )
        s.screenshots.append(Screenshot(
            path=path, name=safe_name, step=last_step,
            url=s.browser._page.url if s.browser._page else "",
        ))
        return f"Element screenshot saved: {path}"

    s._screenshot_counter += 1
    suffix = "_fullpage" if full_page else ""
    safe_name = f"{s._screenshot_counter:03d}_{name}{suffix}"
    path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")
    await s.browser.screenshot(path, full_page=full_page)
    s.screenshots.append(Screenshot(
        path=path, name=safe_name, step=last_step,
        url=s.browser._page.url if s.browser._page else "",
    ))
    return f"Screenshot saved: {path}"


@mcp.tool()
async def screenshot_diff(
    before: str,
    after: str,
    name: str = "diff",
    threshold: int = 25,
) -> str:
    """Compare two screenshots and produce a third image with changed
    regions highlighted in red, so you can see what visually changed
    between two states.

    Useful for detecting layout shifts, content updates that should not
    have happened, focus-ring changes after a click, modal overlays
    appearing, theme switches, etc. Argus does not auto-judge whether
    a diff is a bug — you read the side-by-side and decide.

    Args:
        before: Path or filename of the earlier screenshot (returned
                from a previous `screenshot()` call).
        after: Path of the later screenshot.
        name: Label for the output diff image.
        threshold: 0-255 per-channel pixel difference above which a
                   pixel is considered "changed". Default 25 (mild).
                   Lower = more sensitive.
    """
    from PIL import Image, ImageChops, ImageDraw

    s = _require_session()
    out_dir = Path(_output_dir()) / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(p: str) -> Optional[Path]:
        candidates = [Path(p)]
        if not Path(p).is_absolute():
            candidates.append(out_dir / Path(p).name)
            candidates.append(Path("argus-reports/screenshots") / Path(p).name)
        for c in candidates:
            if c.exists():
                return c
        return None

    before_path = _resolve(before)
    after_path = _resolve(after)
    if before_path is None:
        return f"screenshot_diff: cannot find {before!r}"
    if after_path is None:
        return f"screenshot_diff: cannot find {after!r}"

    try:
        img_a = Image.open(before_path).convert("RGB")
        img_b = Image.open(after_path).convert("RGB")
    except Exception as exc:
        return f"screenshot_diff: failed to open images: {exc}"

    if img_a.size != img_b.size:
        # Resize the smaller to match — keeps comparison meaningful even
        # when viewport changed slightly.
        target = (
            min(img_a.size[0], img_b.size[0]),
            min(img_a.size[1], img_b.size[1]),
        )
        img_a = img_a.resize(target)
        img_b = img_b.resize(target)

    diff_img = ImageChops.difference(img_a, img_b)
    bbox = diff_img.getbbox()

    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    out_path = out_dir / f"{safe_name}.png"

    if bbox is None:
        # Pixel-identical. Save before image as the diff so the agent has
        # a concrete artifact, but signal that there's no change.
        img_a.save(out_path)
        return (
            f"screenshot_diff: images are pixel-identical — no visible change.\n"
            f"  Saved (copy of before): {out_path}"
        )

    # Build a binary mask of changed pixels at the requested threshold.
    grey = diff_img.convert("L")
    mask = grey.point(lambda v: 255 if v > threshold else 0, mode="L")

    # Composite a translucent red overlay onto the after image at the mask.
    red_layer = Image.new("RGB", img_b.size, (255, 0, 0))
    composite = Image.composite(red_layer, img_b, mask).convert("RGB")
    # Blend back so changed regions are tinted, not solid red.
    blended = Image.blend(img_b, composite, 0.5)

    # Outline the overall bounding box for orientation.
    draw = ImageDraw.Draw(blended)
    draw.rectangle(bbox, outline=(255, 0, 0), width=3)

    blended.save(out_path)

    # Heuristic: how much of the image changed?
    changed_pixels = sum(1 for px in mask.getdata() if px > 0)
    total_pixels = mask.size[0] * mask.size[1]
    pct = (changed_pixels / total_pixels) * 100 if total_pixels else 0

    s.screenshots.append(Screenshot(
        path=str(out_path), name=safe_name, step=f"diff: {before} vs {after}",
        url=s.browser._page.url if s.browser._page else "",
    ))

    return (
        f"screenshot_diff saved: {out_path}\n"
        f"  Changed bounding box: {bbox} (size {bbox[2] - bbox[0]} x {bbox[3] - bbox[1]})\n"
        f"  Approx {pct:.1f}% of pixels changed (threshold={threshold}).\n"
        f"  Decide: is this change expected for the action you took? If a UI region "
        f"changed unexpectedly, that may be a bug — call record_bug."
    )


@mcp.tool()
async def get_errors() -> str:
    """Drain captured browser events (console errors, HTTP 4xx/5xx) since
    the last call.

    These two channels are not visible to you through page state, so Argus
    captures them as Bug records automatically. Use this after any action
    that might have triggered a JS error or backend failure.

    For everything else (visual issues, copy problems, missed validation,
    cross-page inconsistency), read the page state yourself and call
    record_bug when you've confirmed something is a real bug.
    """
    s = _require_session()
    console_errs, network_errs = s.browser.drain_errors()

    current_url = s.browser._page.url if s.browser._page else ""
    recent = s.steps[s._steps_since_last_bug:]
    new_bugs = s.detector.process_console_errors(
        console_errs, current_url, recent
    )
    new_bugs.extend(s.detector.process_network_errors(
        network_errs, current_url, recent
    ))

    if new_bugs:
        ss_path = await _auto_screenshot(
            s, f"error_{len(s.bugs) + 1}", f"Error detected on {current_url}"
        )
        for bug in new_bugs:
            bug.screenshot_path = ss_path

    _file_event_bugs(s, new_bugs)

    if not new_bugs and not console_errs and not network_errs:
        return f"No new console or network events. Total bugs in session: {len(s.bugs)}"

    lines = []
    for err in console_errs:
        lines.append(f"[CONSOLE {err['type'].upper()}] {err['text']}")
    for err in network_errs:
        lines.append(f"[HTTP {err['status']}] {err['method']} {err['url']}")
    if new_bugs:
        lines.append(f"\nCaptured {len(new_bugs)} new event-bug(s).")
    lines.append(f"Total bugs in session: {len(s.bugs)}")

    return "\n".join(lines)


_SEVERITY_BY_NAME = {s.value: s for s in Severity}
_BUG_TYPE_BY_NAME = {t.value: t for t in BugType}


@mcp.tool()
async def record_bug(
    title: str,
    severity: str,
    evidence: Optional[dict] = None,
    verify: Optional[dict] = None,
) -> str:
    """Record a confirmed bug you have identified during testing.

    Call this only after you have observed something that meets the bug
    bar: reproducible, user-affecting, persistent. Do not record
    speculation or polish nits. The session report is built from these
    records — be specific.

    Args:
        title: One-line headline, specific. Bad: "Form has issues."
               Good: "Login form accepts any password — no authentication."
        severity: "critical" | "high" | "medium" | "low" | "info".
                  HIGH = data loss / security / payment / blocked flow.
                  MEDIUM = workflow friction / confusing UX / cross-page bug.
                  LOW = polish / suggestion-grade.
        verify: Optional reproduction clause. When the bug has a
            machine-checkable symptom (something present/absent on a fresh
            page load), pass it and Argus will INDEPENDENTLY re-load the
            page and confirm the symptom before recording — turning the
            bug into a verified, reproducible finding instead of your
            unverified say-so. This is Argus's anti-false-positive guard;
            use it whenever the symptom is text-checkable. Shape:
                {"expect": "present"|"absent",
                 "target_text": "the text that proves the bug",
                 "at_url": "/path"}   # optional, defaults to current page
            Examples:
              - Fake delete (item survives): {"expect":"present",
                "target_text":"Buy groceries","at_url":"/tasks"}
              - Save didn't persist (new value missing): {"expect":"absent",
                "target_text":"EDITED-XYZ","at_url":"/tasks/1/edit"}
            For a MULTI-STEP bug (the symptom only appears after a journey),
            add "replay": true — Argus re-drives the recorded action trace
            (click_what/type_into/select_into/navigate) in a fresh cold context
            and checks the symptom there, giving a stronger "reproduced by
            replaying N steps from a cold start" receipt (or INCONCLUSIVE if a
            step no longer resolves). Shape:
                {"replay": true, "expect": "present"|"absent",
                 "target_text": "the text that proves the bug"}
            CAUTION: replay re-EXECUTES the journey's steps against the live
            backend, so any Save/Delete/Add/checkout in the trace runs a second
            time (real side effect; the receipt reports writes_replayed). Use the
            plain clean-load verify (no replay) for destructive flows, or accept
            the re-run.
            Add "minimize": true to also narrow a confirmed reproduction to the
            minimal sufficient steps ("you don't need all 7 — 2 and 5 suffice").
            Minimization runs ONLY for a write-free journey (it re-runs subsets,
            which would repeat any writes); it is skipped with a note otherwise.
            Omit verify entirely for visual/layout/UX-judgment bugs that no
            single text check captures — those record as observation-based.
        evidence: Optional dict with extra context. Recommended keys:
            description (str): Longer explanation including user impact.
                Default = same as title.
            steps (list[str]): Reproduction steps. Default = current
                session step log (everything you did so far).
            url (str): Page or screen URL. Default = current page URL.
            screenshot (str): One of "auto" (default — take one now and
                attach), "skip" (no screenshot), or a label to use as
                the screenshot filename. Pre-existing screenshot paths
                are also accepted.
            bug_type (str): A category for the report. Default
                "ux_issue". One of: console_error, network_error,
                visual_anomaly, ux_issue, crash, broken_link,
                form_error, state_verification, misleading_success,
                count_mismatch, text_anomaly, broken_image, seo_issue,
                accessibility, performance, mixed_content.
    """
    s = _require_session()

    sev_key = (severity or "").strip().lower()
    if sev_key not in _SEVERITY_BY_NAME:
        return (
            f"record_bug: invalid severity {severity!r}. "
            f"Use one of: {', '.join(_SEVERITY_BY_NAME)}."
        )
    sev = _SEVERITY_BY_NAME[sev_key]

    # Be forgiving about `evidence`: agents (esp. weaker models) pass a bare
    # string instead of a dict. A string used to crash record_bug
    # ('str' has no .get) — losing the whole finding. Treat a string as the
    # description; anything non-dict becomes empty.
    if isinstance(evidence, dict):
        ev = evidence
    elif isinstance(evidence, str) and evidence.strip():
        ev = {"description": evidence.strip()}
    else:
        ev = {}
    description = ev.get("description") or title
    # Default to the steps taken *since the last record_bug* — otherwise
    # consecutive bug reports accumulate earlier bugs' actions and the
    # reproducible-steps section reads as session noise.
    if ev.get("steps") is not None:
        steps = list(ev["steps"])
    else:
        steps = list(s.steps[s._steps_since_last_bug:])
    if ev.get("url"):
        url = ev["url"]
    elif s.mode == "web" and s.browser is not None and s.browser._page is not None:
        url = s.browser._page.url
    elif s.mode == "screen" and s.screen is not None and s.screen._app_name:
        url = f"screen://{s.screen._app_name}"
    else:
        url = ""

    type_key = (ev.get("bug_type") or "ux_issue").strip().lower()
    if type_key not in _BUG_TYPE_BY_NAME:
        return (
            f"record_bug: invalid bug_type {type_key!r}. "
            f"Use one of: {', '.join(_BUG_TYPE_BY_NAME)}."
        )
    bug_type = _BUG_TYPE_BY_NAME[type_key]

    screenshot_directive = ev.get("screenshot", "auto")
    screenshot_path: Optional[str] = None
    if screenshot_directive == "skip":
        pass
    elif screenshot_directive in ("auto", "", None):
        label = "bug_" + "".join(c if c.isalnum() else "_" for c in title.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {title[:60]}")
    elif "/" in screenshot_directive or screenshot_directive.endswith(".png"):
        # Treat as an existing path
        screenshot_path = screenshot_directive
    else:
        # Treat as a label for a fresh screenshot
        label = "".join(c if c.isalnum() else "_" for c in screenshot_directive.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {title[:60]}")

    # Lower the barrier to engaging the receipt: if no explicit verify clause was
    # passed but the evidence dict carries a checkable target, build one from it.
    # (The agent still supplies the target — we never GUESS it, which would risk
    # a false VERIFIED; we only accept it from a second, more natural place.)
    if verify is None and (ev.get("target_text") or ev.get("target")):
        verify = {
            "expect": (ev.get("expect") or "present"),
            "target_text": ev.get("target_text") or ev.get("target"),
            "at_url": ev.get("at_url") or ev.get("after_url") or "",
            "replay": bool(ev.get("replay")),
        }

    replay_slice = list(s.action_trace[s._actions_since_last_bug:])
    if verify and verify.get("replay"):
        receipt = await _run_replay_receipt(s, verify, replay_slice)
    elif verify:
        receipt = await _run_reproduction_check(s, verify)
    else:
        receipt = None

    # If a state capsule is in play, flag the finding ONLY when there's positive
    # evidence the session died — the marker is absent AND the page is a login
    # wall. (A marker simply not rendering on a legitimate authenticated page —
    # checkout, settings, a modal — must not falsely flag a real bug.) When it
    # does fire, also neutralize the receipt so an expired-session artifact can't
    # read as VERIFIED.
    if (s._capsule_marker and s.mode == "web" and s.browser is not None
            and s.browser._page is not None):
        try:
            cur = await s.browser.get_state()
            if not _marker_visible(s._capsule_marker, cur) and _looks_logged_out(cur):
                description = (description.rstrip()
                              + "\n\n[UNRELIABLE: the restored state capsule's session appears expired "
                                "(login wall present, marker absent) — re-mint state and re-confirm.]")
                if isinstance(receipt, dict) and receipt.get("attempted"):
                    receipt = {**receipt, "reproduced": None,
                               "reason": "recorded against an expired capsule session"}
        except Exception:
            pass

    bug = Bug(
        type=bug_type,
        severity=sev,
        title=title,
        description=description,
        url=url,
        steps_to_reproduce=list(steps),
        screenshot_path=screenshot_path,
        reproduction_receipt=receipt,
        replay_steps=replay_slice,
    )
    s.bugs.append(bug)
    s.steps.append(f"record_bug: [{sev.value}] {title}")
    # Reset the per-bug cursors so the *next* record_bug only carries actions
    # taken between this call and that one.
    s._steps_since_last_bug = len(s.steps)
    s._actions_since_last_bug = len(s.action_trace)

    out = [
        f"Recorded bug [{sev.value.upper()}] {title}",
        f"  url: {url}",
        f"  type: {bug_type.value}",
        f"  steps: {len(steps)} step(s)",
    ]
    if screenshot_path:
        out.append(f"  screenshot: {screenshot_path}")
    if receipt is not None:
        is_replay = receipt.get("mode") == "replay"
        if not receipt.get("attempted"):
            out.append(f"  reproduction: not run — {receipt.get('reason', 'n/a')}")
        elif receipt.get("reproduced") is True:
            if is_replay:
                out.append(f"  reproduction: CONFIRMED by replaying {receipt.get('steps')} step(s) "
                           f"from a cold start ({receipt['expect']} {receipt['target_text']!r})")
                if receipt.get("minimal_count") is not None and receipt["minimal_count"] < receipt.get("steps", 0):
                    out.append(f"  minimal repro: {receipt['minimal_count']} of {receipt['steps']} step(s) "
                               f"suffice — {receipt.get('minimal_steps')}")
                elif receipt.get("minimize_skipped"):
                    out.append(f"  minimize: {receipt['minimize_skipped']}")
            else:
                out.append(f"  reproduction: CONFIRMED {receipt['runs']} from clean load "
                           f"({receipt['expect']} {receipt['target_text']!r} @ {receipt['at_url']})")
        elif receipt.get("reproduced") is False:
            if is_replay:
                out.append(f"  reproduction: NOT REPRODUCED — replayed {receipt.get('steps')} step(s) "
                           "but the symptom was absent. Re-check before trusting this.")
            else:
                tag = "FLAKY" if receipt.get("flaky") else "NOT REPRODUCED"
                out.append(f"  reproduction: {tag} {receipt['runs']} on clean reload — "
                           "the symptom you reported did not hold up. Re-check before trusting this.")
        else:  # None — inconclusive
            if is_replay and receipt.get("diverged"):
                out.append("  reproduction: INCONCLUSIVE — replay path diverged (a recorded step no "
                           "longer resolves), so the journey couldn't be re-driven. Not a confirmation.")
            elif is_replay:
                out.append("  reproduction: INCONCLUSIVE — the symptom already held before the journey, "
                           "so it can't be attributed to these steps. Not a confirmation.")
            else:
                out.append(f"  reproduction: check errored — {receipt.get('error', 'unknown')}")
    else:
        # No receipt: nudge toward one for the (common) text-checkable case so
        # the precision moat actually engages — a weaker agent won't otherwise.
        out.append("  reproduction: NONE — this finding is unverified (your say-so). If the "
                   "symptom is text-checkable (an item present/absent, a wrong count, a lying "
                   "toast), re-record with verify={\"expect\":\"present|absent\",\"target_text\":"
                   "\"...\",\"at_url\":\"/path\"} so Argus re-confirms it from a clean load.")
    out.append(f"  total bugs in session: {len(s.bugs)}")
    return "\n".join(out)


@mcp.tool()
async def verify_persistence(
    expect: str,
    target_text: str,
    after_url: str = "",
) -> str:
    """Force a fresh page load and report whether `target_text` is present
    or absent — your tool for catching the "Saved!" toast that lied.

    After any destructive or persistence-changing action (delete, edit,
    save, submit, toggle, payment, etc.), the success toast is not
    proof. Only a fresh GET on the relevant page is. This tool does
    that GET and reports presence — you decide whether the result
    matches what you expected.

    Examples:
      verify_persistence("absent", "Buy groceries", "/tasks")
        — after deleting "Buy groceries", confirm it's gone from the list.
      verify_persistence("present", "EDITED-VALUE-XYZ", "/tasks/1/edit")
        — after editing, confirm the new value reloads.

    Argus does not auto-record a bug here. If presence does not match
    your expectation, call record_bug.

    Args:
        expect: "present" or "absent" — what state the target_text
                should be in after the fresh page load.
        target_text: The text or value you're checking for.
        after_url: Page to load and inspect. Defaults to the current URL.
    """
    s = _require_session()
    expect_norm = (expect or "").strip().lower()
    if expect_norm not in ("present", "absent"):
        return (
            f"verify_persistence: invalid `expect` {expect!r}. "
            f"Use 'present' or 'absent'."
        )

    s.steps.append(f'verify_persistence({expect_norm}, {target_text[:60]!r})')

    current_url = s.browser._page.url if s.browser._page else ""
    nav_url = _resolve_url(s, after_url) if after_url else current_url
    await s.browser.goto(nav_url)
    after_state = await s.browser.get_state()
    s._last_elements = after_state.elements

    present = _text_in_state(target_text, after_state)
    matches = (present and expect_norm == "present") or (
        not present and expect_norm == "absent"
    )

    if expect_norm == "absent":
        verdict = (
            "Target text is GONE after refresh — matches expectation."
            if not present
            else "Target text is STILL PRESENT after refresh — does NOT match expectation."
        )
    else:
        verdict = (
            "Target text is PRESENT after refresh — matches expectation."
            if present
            else "Target text is MISSING after refresh — does NOT match expectation."
        )

    return (
        f"verify_persistence(expect={expect_norm!r}, target={target_text[:60]!r}) "
        f"on {nav_url}\n"
        f"  Result: {'MATCH' if matches else 'MISMATCH'} — {verdict}\n"
        f"  Decide: if this mismatch is a real bug (silent delete failure, "
        f"edit not persisting, etc.), call record_bug. Argus does not infer "
        f"that for you."
    )


@mcp.tool()
async def check_links() -> str:
    """Probe every internal link on the current page (HEAD with GET fallback)
    and return raw status-code results.

    Argus does not auto-record bugs here. You read the dead-link list and
    decide. A handful of dead anchor links is rarely shippable; the
    severity depends on context. Call record_bug for the ones that matter.
    """
    s = _require_session()
    state = await s.browser.get_state()
    s._last_elements = state.elements

    link_results = await s.browser.check_links(state.links)
    dead = [r for r in link_results if not r["ok"]]
    alive = [r for r in link_results if r["ok"]]
    external = [l for l in state.links if not l.get("isInternal")]

    lines = [f"Checked {len(link_results)} internal links on {state.url}"]
    lines.append(f"  OK: {len(alive)}")
    lines.append(f"  Dead: {len(dead)}")
    lines.append(f"  External (not checked): {len(external)}")
    if dead:
        lines.append("")
        for r in dead:
            lines.append(f"  [HTTP {r['status']}] {r['href']}")
        lines.append("")
        lines.append("Decide: are any of these real bugs? Call record_bug if so.")
    return "\n".join(lines)


@mcp.tool()
async def check_performance() -> str:
    """Read raw performance metrics from the browser's Performance API
    (load time, TTFB, request count, large resources).

    Argus does not auto-record bugs here — Lighthouse already owns the
    performance-audit space. Only call record_bug if the page is so slow
    or so heavy that it materially blocks a real user (multi-second TTFB
    on a primary flow, multi-MB hero asset, etc).
    """
    s = _require_session()
    current_url = s.browser._page.url if s.browser._page else ""
    perf_data = await s.browser.get_performance()

    nav = perf_data.get("navigation", {})
    summary = perf_data.get("summary", {})
    lines = [f"Performance for {current_url}"]
    if nav:
        lines.append(f"  Load time: {nav.get('loadTime', 0)/1000:.2f}s")
        lines.append(f"  TTFB: {nav.get('ttfb', 0)/1000:.2f}s")
        lines.append(f"  DOM interactive: {nav.get('domInteractive', 0)/1000:.2f}s")
    lines.append(f"  Total requests: {summary.get('totalRequests', '?')}")
    lines.append(f"  Total size: {summary.get('totalSize', 0)/1024:.0f} KB")
    large = perf_data.get("resources", [])
    if large:
        lines.append(f"  Large resources (>500KB):")
        for r in large:
            lines.append(f"    {r['size']/1024:.0f}KB — {r['name'][:80]}")
    return "\n".join(lines)


@mcp.tool()
async def crawl_site(max_pages: int = 20) -> str:
    """Discover pages: crawl internal links from the current page, auto-capturing
    only console/network events (tagged auto-captured) and checking links per page.

    This is page DISCOVERY, not a judgment pass — it does not decide what's a bug
    for you. Walk the surfaced pages with observe() and record_bug what you confirm.
    Can take 30-120 seconds depending on site size.

    Args:
        max_pages: Maximum number of pages to visit (default 20)
    """
    from urllib.parse import urlparse, urlunparse

    s = _require_session()
    start_url = s.browser._page.url if s.browser._page else s.url
    visited: set = set()
    visited_paths: set = set()  # track by path to avoid ?ref=x duplicates
    to_visit: list = [start_url]
    page_results: list = []

    def _normalize(u: str) -> str:
        """Strip query params and fragments for dedup."""
        p = urlparse(u)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        normalized = _normalize(url)
        if normalized in visited_paths:
            continue
        visited.add(url)
        visited_paths.add(normalized)

        try:
            await s.browser.goto(url)
        except Exception:
            continue

        state = await s.browser.get_state()
        s._last_elements = state.elements
        if state.url not in s.pages_visited:
            s.pages_visited.append(state.url)

        # Capture only what the agent cannot see directly: console + network events.
        console_errs, network_errs = s.browser.drain_errors()
        recent = s.steps[s._steps_since_last_bug:]
        new_bugs = s.detector.process_console_errors(console_errs, state.url, recent)
        new_bugs.extend(s.detector.process_network_errors(network_errs, state.url, recent))

        # Probe links once (raw probe, no auto-bug — agent decides).
        link_results = await s.browser.check_links(state.links)
        dead = [r for r in link_results if not r["ok"]]

        # Screenshot when console/network captured something.
        if new_bugs:
            page_name = state.url.split("/")[-1] or "index"
            ss_path = await _auto_screenshot(s, f"crawl_{page_name}", f"Crawl: {state.url}")
            for bug in new_bugs:
                bug.screenshot_path = ss_path

        _file_event_bugs(s, new_bugs)
        page_results.append((state.url, len(new_bugs), len(dead)))

        # Discover new internal links to visit (deduplicated by path)
        for link in state.links:
            href = link.get("href", "")
            if link.get("isInternal") and _normalize(href) not in visited_paths and href not in to_visit:
                to_visit.append(href)

    lines = [f"Crawl complete: {len(visited)} pages visited, {len(s.bugs)} event-bugs captured"]
    lines.append("")
    for url, bug_count, dead_count in page_results:
        markers = []
        if bug_count:
            markers.append(f"{bug_count} event-bug(s)")
        if dead_count:
            markers.append(f"{dead_count} dead link(s)")
        marker = f"  ({', '.join(markers)})" if markers else ""
        lines.append(f"  {url}{marker}")
    lines.append("")
    lines.append("Crawl auto-records only console/network events. For everything")
    lines.append("else, observe the pages yourself and call record_bug.")
    return "\n".join(lines)


# ── Compound Action Tools ─────────────────────────────────────────


@mcp.tool()
async def test_action(target: str, expectation: str = "", expect: Optional[dict] = None) -> str:
    """Click an element and observe the diff in one round-trip — optionally
    predicting the outcome so Argus can catch the SURPRISE for you.

    Captures the state before and after a click on the element matching
    `target`, computes a structural diff, drains console/network events,
    and screenshots the result.

    Pass `expect` to commit a machine-checkable prediction in user-observable
    terms; Argus reports each as MATCH or SURPRISE against the real diff. This
    is the senior-tester move — a SURPRISE on something nobody scripted is a
    bug lead (fake delete, off-by-one count). It is an in-session OBSERVATION,
    not a reproduced finding: to bank it, call record_bug with a verify clause.

    Args:
        target: Natural-language description of the element to click,
                same syntax as click_what ("Login button", "Add Task").
        expectation: Optional human note (informational, shown in the diff).
        expect: Optional bounded prediction dict, any of:
            {"count": {"label": "tasks", "delta": 1}}   count delta (or "value")
            {"gains": "Buy milk"} / {"removes": "Buy milk"}  list membership
            {"text_present": "Saved"} / {"text_absent": "Buy milk"}
            {"toast": "Saved"}                          a new toast appeared
            {"url_changed": true}
          Multiple keys may be combined; all must hold.
    """
    s = _require_session()

    el, err = _resolve_or_error(s, target)
    if err:
        return err

    label = el.text or el.aria_label or el.placeholder or el.name or el.tag
    step = f'test_action({target!r}) -> "{label[:60]}"'
    if expectation:
        step += f" — expected: {expectation}"
    s.steps.append(step)

    s.browser.drain_errors()
    before = await s.browser.get_state()
    net_pre = len(s.browser.network_log)  # bracket the requests THIS click fires

    try:
        # Route through _locator so a duplicate-label target hits the resolved
        # element, not the first DOM match (same fix as click_what).
        idx = s._last_elements.index(el)
        await s.browser._locator(idx, s._last_elements).click(timeout=5000)
        await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as exc:
        ss = await _auto_screenshot(s, "action_failed", step)
        return (
            f"test_action({target!r}) — click failed: {exc}\n"
            f"The element may be obscured, stale, or removed. "
            f"Screenshot: {ss}"
        )

    await asyncio.sleep(0.3)
    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)
    _record_action(s, "click_what", target)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _capture_browser_events(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, expectation or target)

    ss_path = await _auto_screenshot(s, f"action_{label[:20]}", step)
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    _file_event_bugs(s, new_bugs)

    lines = [
        f'ACTION: Clicked "{label[:60]}" via target {target!r}',
    ]
    if expectation:
        lines.append(f"  Expected: {expectation}")
    lines.append("")
    lines.append("CHANGES:")
    for c in changes:
        lines.append(f"  {c}")
    lines.append("")

    if console_errs or network_errs:
        lines.append("BROWSER EVENTS:")
        for err in console_errs[:3]:
            lines.append(f"  [CONSOLE] {err['text'][:80]}")
        for err in network_errs[:3]:
            lines.append(f"  [HTTP {err['status']}] {err['method']} {err['url'][:60]}")
    else:
        lines.append("BROWSER EVENTS: none")

    if new_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(new_bugs)} new):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")

    new_reqs = s.browser.network_log[net_pre:]
    xs_evidence, xs_check = _reconcile_action(new_reqs, before, after)
    if new_reqs or xs_check:
        lines.append("")
        lines.append("CROSS-STACK (UI claim vs wire):")
        for e in xs_evidence:
            lines.append(f"  {e}")
        if xs_check:
            lines.append(f"  CHECK: {xs_check}")

    if expect:
        checks = _evaluate_expectation(before, after, expect)
        surprises = sum(1 for _, ok, _ in checks if ok is False)
        unchecked = sum(1 for _, ok, _ in checks if ok is None)
        lines.append("")
        lines.append("EXPECTATION CHECK:")
        for label, ok, detail in checks:
            tag = "MATCH    " if ok is True else ("SURPRISE " if ok is False else "UNCHECKED")
            lines.append(f"  [{tag}] {label}" + (f"  ({detail})" if detail else ""))
        if not checks:
            lines.append("  (no recognised predicate keys — see expect= docs)")
        elif surprises:
            lines.append(
                f"\n{surprises} SURPRISE(S): the page did not do what you predicted — a "
                "bug lead. To bank it, record_bug with a verify clause so it gets a "
                "reproduction receipt (this check is an in-session observation, not a receipt)."
            )
        elif unchecked:
            lines.append(
                f"\nAll measurable predictions held, but {unchecked} could NOT be evaluated "
                "(see UNCHECKED above) — fix the predicate or verify those by hand."
            )
        else:
            lines.append("\nAll predictions held.")

    lines.append(
        "\nDecide: did anything you observed warrant a record_bug call? "
        "test_action does not infer that for you."
    )
    lines.append(f"Screenshot: {ss_path}")
    return "\n".join(lines)


@mcp.tool()
async def test_form(
    form_fields: dict,
    submit: str = "auto",
) -> str:
    """Fill a set of form fields and submit, in one call. Description-keyed.

    Each key in `form_fields` is matched to an input via the same
    natural-language resolver used by type_into / select_into. Use
    descriptions that match the field's label, name, or placeholder:
    {"email": "alice@x.com", "password": "abc12345", "confirm": "abc12345"}.

    Argus reports what happened (URL change, new feedback messages,
    captured browser events, structural diff). It does NOT label the
    outcome as success / failure — your job is to read the result and
    call record_bug if you've confirmed a real bug.

    Args:
        form_fields: Dict {field_description: value}.
        submit: How to submit. "auto" (default) finds the most likely
            submit button by text. "enter" presses Enter on the last
            filled field. Otherwise, treated as a description for
            click_what to resolve (e.g. "Save Changes" or "Register").
    """
    s = _require_session()

    s.browser.drain_errors()
    before = await s.browser.get_state()
    before_url = before.url
    s._last_elements = before.elements

    field_results = []
    for field_desc, value in form_fields.items():
        # Re-observe before each field; SPA re-renders shift element refs.
        state = await s.browser.get_state()
        s._last_elements = state.elements

        result = resolve_element(field_desc, state.elements, kind_filter="input")
        if result.reason != "unique" or result.found is None:
            # Try without the kind filter — sometimes selects or buttons are intended
            result_any = resolve_element(field_desc, state.elements)
            if result_any.reason != "unique" or result_any.found is None:
                field_results.append(f'[MISS] {field_desc!r} — {result.reason}')
                continue
            el = result_any.found
        else:
            el = result.found

        selector = s.browser._build_selector(el)
        try:
            if el.tag == "select":
                await s.browser._page.select_option(selector, str(value), timeout=5000)
            else:
                await s.browser._page.fill(selector, str(value), timeout=5000)
            field_results.append(f'[OK] {field_desc!r} -> {value!r}')
            s.steps.append(f'Type {value!r} into {field_desc!r}')
        except Exception as exc:
            field_results.append(f'[FAIL] {field_desc!r} — {exc}')

    # Submit
    state = await s.browser.get_state()
    s._last_elements = state.elements
    submit_label = ""

    if submit == "enter":
        # Press Enter on whatever element is currently focused, falling back
        # to the last filled field if we can determine it.
        try:
            await s.browser._page.keyboard.press("Enter")
            await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
            submit_label = "Enter key"
            s.steps.append("Press Enter to submit")
        except Exception as exc:
            ss = await _auto_screenshot(s, "form_no_submit", "Enter-key submit failed")
            return f"FORM: filled {len(field_results)} fields but Enter-submit failed: {exc}"
    else:
        if submit == "auto":
            # Auto-discover: prefer a button whose text matches typical submit verbs.
            submit_keywords = [
                "submit", "save", "create", "sign in", "log in", "login",
                "register", "send", "add", "continue",
            ]
            scored: list[tuple[int, InteractiveElement]] = []
            for el in state.elements:
                if el.tag not in ("button", "input"):
                    continue
                if el.tag == "input" and el.type not in ("submit", "button"):
                    continue
                text = (el.text or "").lower()
                score = 0
                for kw in submit_keywords:
                    if kw in text:
                        score = max(score, 50 + len(kw))
                if el.type == "submit":
                    score = max(score, 80)
                if score > 0:
                    scored.append((score, el))
            scored.sort(key=lambda p: -p[0])
            target_el = scored[0][1] if scored else None
        else:
            result = resolve_element(submit, state.elements, kind_filter="button")
            if result.reason != "unique" or result.found is None:
                return (
                    f"FORM: filled {len(field_results)} fields but submit "
                    f"description {submit!r} did not resolve: {result.reason}.\n"
                    + "\n".join(field_results)
                )
            target_el = result.found

        if target_el is None:
            ss = await _auto_screenshot(s, "form_no_submit", "Could not find submit button")
            return (
                f"FORM: filled {len(field_results)} fields but no submit button "
                f"found. Pass submit=\"enter\" or submit=\"<description>\" "
                f"explicitly.\n" + "\n".join(field_results)
            )

        submit_label = target_el.text or target_el.aria_label or "submit"
        s.steps.append(f'Click {submit_label!r}')
        sel = s.browser._build_selector(target_el)
        try:
            await s.browser._page.click(sel, timeout=5000)
            await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as exc:
            ss = await _auto_screenshot(s, "form_submit_fail", "Submit click failed")
            return (
                f"FORM: filled {len(field_results)} fields but submit click failed: {exc}\n"
                + "\n".join(field_results)
            )

    await asyncio.sleep(0.3)

    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _capture_browser_events(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, "form submission")

    redirected = after.url != before_url
    ss_path = await _auto_screenshot(s, "form_result", f"Form: {submit_label}")
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    _file_event_bugs(s, new_bugs)

    lines = [
        f'FORM SUBMISSION via "{submit_label}"',
        "",
        "FIELDS:",
    ]
    for fr in field_results:
        lines.append(f"  {fr}")
    lines.append("")
    lines.append("RESULT:")
    lines.append(
        f"  URL: {before_url} -> {after.url} "
        f"{'(redirected)' if redirected else '(same page)'}"
    )
    if after.toast_messages:
        lines.append(f"  Feedback: {', '.join(after.toast_messages[:3])}")
    lines.append("")
    lines.append("CHANGES:")
    for c in changes:
        lines.append(f"  {c}")

    if console_errs or network_errs:
        lines.append("")
        lines.append("BROWSER EVENTS:")
        for err in console_errs[:3]:
            lines.append(f"  [CONSOLE] {err['text'][:80]}")
        for err in network_errs[:3]:
            lines.append(f"  [HTTP {err['status']}] {err['method']} {err['url'][:60]}")

    if new_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(new_bugs)} new):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")

    lines.append(
        "\nDecide: was the outcome what you expected? If the form accepted "
        "garbage / lost data / showed a misleading toast / etc., call record_bug."
    )
    lines.append(f"Screenshot: {ss_path}")
    return "\n".join(lines)



@mcp.tool()
async def regression_check() -> str:
    """Re-test previously-recorded findings for this site against the CURRENT
    build — "did my fix land, and did anything I'd fixed come back?".

    Findings with a clean-load verify clause are journaled at end_session (per
    origin). This re-runs each one's INDEPENDENT clean-load check now and
    classifies it: STILL-PRESENT (the bug is still there), NO-LONGER-REPRODUCES
    (the symptom is gone — likely fixed; confirm the surface still exists), or
    INCONCLUSIVE. Each carried finding is treated as a hypothesis and re-checked
    from scratch — nothing is trusted from the prior run. Read-only (clean GETs);
    replay-mode findings are not auto-re-driven (that would re-execute writes).
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or s.browser._page is None:
        return "regression_check: this tool is web-mode only."
    origin = (urlparse(s.browser._page.url).netloc
              or urlparse(s.url or "").netloc or "default")
    entries = _journal_entries(origin)
    if not entries:
        return (f"regression_check: no journaled findings for {origin}. Findings with a "
                "verify clause are journaled when you call end_session.")

    lines = [f"Regression re-check for {origin} — {len(entries)} journaled finding(s):", ""]
    still = gone = incon = 0
    for e in entries:
        receipt = await _run_reproduction_check(s, e.get("verify") or {})
        rep = receipt.get("reproduced")
        if rep is True:
            tag, n = "STILL-PRESENT       ", "still"
            still += 1
        elif rep is False:
            tag, n = "NO-LONGER-REPRODUCES", "gone"
            gone += 1
        else:
            tag, n = "INCONCLUSIVE        ", "incon"
            incon += 1
        lines.append(f"  [{tag}] [{e.get('severity', '?').upper()}] {e.get('title', '')[:80]}")
    lines.append("")
    lines.append(f"{still} still present · {gone} no longer reproduce (likely fixed — "
                 f"confirm the surface still exists) · {incon} inconclusive.")
    return "\n".join(lines)


@mcp.tool()
async def end_session() -> str:
    """End the testing session, close the browser, and generate an HTML error report.

    Returns the path to the generated report and a summary of findings.
    """
    s = _require_session()

    if s.mode == "web":
        # Persist re-checkable findings so a later run can regression-test them.
        try:
            _write_journal(s)
        except Exception:
            pass

    if s.mode == "web" and s.browser is not None:
        # No blind final drain here: auto-filing console/network bugs after the
        # agent has stopped means they can never be judged or re-confirmed.
        # Events captured during the session were already filed (tagged
        # auto-captured) via get_errors / test_action / test_form / crawl_site.
        await s.browser.stop()
    elif s.mode == "screen" and s.screen is not None:
        await s.screen.stop()

    duration = asyncio.get_event_loop().time() - (s.start_time or 0)

    target = s.url if s.mode == "web" else f"screen://{s.mode}"
    result = ExplorationResult(
        url=target or "",
        bugs=s.bugs,
        pages_visited=s.pages_visited,
        actions_taken=len(s.steps),
        duration_seconds=duration,
        focus_areas=s.focus_areas,
        screenshots=s.screenshots,
    )

    output_dir = _output_dir()
    reporter = Reporter()
    report_path = reporter.generate(result, output_dir)

    # Reset session
    global _session
    _session = Session()

    # Build summary
    lines = [
        f"Session ended. Report saved: {report_path}",
        f"",
        f"Summary:",
        f"  Actions taken: {result.actions_taken}",
        f"  Pages visited: {len(result.pages_visited)}",
        f"  Bugs found: {len(result.bugs)}",
        f"  Screenshots: {len(result.screenshots)}",
        f"  Duration: {duration:.1f}s",
    ]
    if result.bugs:
        lines.append("")
        lines.append("Bugs:")
        for bug in result.bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title}")

    return "\n".join(lines)


def _argus_version() -> str:
    """The Argus version — single source is argus.__version__."""
    from . import __version__
    return __version__


def main():
    """Entry point for argus-mcp command.

    Flags:
      --version      Print the installed Argus version and exit. Useful
                     after `pip install -U` to confirm the running MCP
                     server picks up the new tools (you usually need to
                     restart your MCP host to refresh the tool table).
      --unsafe       Enable eval_js (off by default; can read cookies,
                     mutate state, and fetch arbitrary URLs from the
                     page context).
      --doctor       Run the macOS screen-mode permission check and
                     exit. Use this before launching screen mode for
                     the first time.

    Without flags, just runs the MCP server over stdio.
    """
    import sys as _sys

    if "--version" in _sys.argv or "-V" in _sys.argv:
        print(f"argus-testing {_argus_version()}")
        _sys.exit(0)

    if "--doctor" in _sys.argv:
        from .screen.permissions import main as _doctor
        _sys.exit(_doctor())

    if "--unsafe" in _sys.argv:
        os.environ["ARGUS_UNSAFE_EVAL"] = "1"
        _sys.argv = [a for a in _sys.argv if a != "--unsafe"]

    mcp.run()


if __name__ == "__main__":
    main()
