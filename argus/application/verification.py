from __future__ import annotations

from argus.domain.models import (
    PageState,
)

from .evidence import _auto_screenshot
from .session import Session
from .text import _present_with_scroll, _resolve_url, _visible_text_in_state

_LOGOUT_PHRASES = (
    "please log in",
    "please sign in",
    "log in to",
    "sign in to",
    "session expired",
    "session has expired",
    "you have been logged out",
    "you are logged out",
    "log in to continue",
    "sign in to continue",
)


def _looks_logged_out(state: PageState) -> bool:
    """Heuristic: the page is a login wall (so an auth-gated symptom is absent
    because we're logged out, not because the bug was fixed). Conservative full
    phrases, not the bare word 'login' (which appears on logged-in pages too)."""
    t = (state.page_text or "").lower()
    return any(p in t for p in _LOGOUT_PHRASES)


async def _run_reproduction_check(
    s: "Session", verify: dict, actions: list[dict] | None = None
) -> dict:
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
    localStorage/sessionStorage by default: a symptom that lives purely in client
    web storage can survive the reload, so an `expect=absent` claim against
    client-only state may read present. This is the right boundary for the
    common case (don't log the agent out), but it means the receipt proves
    "server-backed truth", not "all client state cleared". Pass
    verify["clear_storage"]=True to wipe client storage before the reload and
    prove SERVER persistence (catches a "save" that only wrote localStorage).

    Returns a receipt dict; never raises.
    """
    expect = (verify.get("expect") or "").strip().lower()
    target = (verify.get("target_text") or verify.get("target") or "").strip()
    at_url = (verify.get("at_url") or verify.get("after_url") or "").strip()
    expected_status = verify.get("expect_status")

    if expected_status is not None:
        try:
            expected_status = int(expected_status)
        except (TypeError, ValueError):
            return {"attempted": False, "reason": "expect_status must be an HTTP status integer"}
        if not 100 <= expected_status <= 599:
            return {"attempted": False, "reason": "expect_status must be between 100 and 599"}

    has_text_check = expect in ("present", "absent") and bool(target)
    if expected_status is None and not has_text_check:
        return {
            "attempted": False,
            "reason": "verify needs expect_status or expect in {present, absent} with target_text",
        }
    if expected_status is not None and (expect or target):
        return {
            "attempted": False,
            "reason": "use either expect_status or expect/target_text, not both",
        }
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return {"attempted": False, "reason": "reproduction re-check is only available in web mode"}

    restore_url = s.browser.current_url
    nav_url = _resolve_url(s, at_url) if at_url else restore_url
    observations = []
    observed_statuses = []
    # A clean load means none of the agent's own forced responses are live —
    # otherwise an injected 500 re-fires on reload and certifies itself.
    suspended_mocks = await s.browser.suspend_mocks()
    inconclusive = None
    evidence = {}
    try:
        if verify.get("clear_storage"):
            # Opt-in: prove SERVER persistence by wiping client storage first, so
            # a localStorage-only "save" doesn't survive the reload and certify.
            await s.browser.goto(nav_url)
            await s.browser.clear_client_storage()
        for _ in range(2):
            resp = await s.browser.goto(nav_url)
            status = resp.status if resp is not None else None
            observed_statuses.append(status)
            if expected_status is not None:
                observations.append(status == expected_status)
                continue
            state = await s.browser.get_state()
            # A 4xx/5xx page or a login wall makes the symptom absent for the
            # wrong reason — never certify (esp. expect=absent) off that.
            if resp is not None and resp.status >= 400:
                inconclusive = (
                    f"clean load returned HTTP {resp.status} — can't attribute the symptom"
                )
                break
            if _looks_logged_out(state):
                inconclusive = "clean load hit a login wall — the session may have expired"
                break
            observations.append(await _present_with_scroll(s, target, state))
        evidence["evidence_url"] = s.browser.current_url
        if verify.get("_screenshot_label"):
            try:
                # Capture before restoring the caller's page, without another navigation.
                path = await _auto_screenshot(
                    s, verify["_screenshot_label"], "fresh-load verification"
                )
                if path:
                    evidence["screenshot_path"] = path
                else:
                    evidence["evidence_warning"] = "Verification screenshot was unavailable."
            except Exception:
                evidence["evidence_warning"] = "Verification screenshot capture failed."
    except Exception as e:  # navigation/render failure — report, don't crash record_bug
        return {
            "attempted": True,
            "reproduced": None,
            "at_url": nav_url,
            "error": str(e)[:200],
            "mocks_suspended": suspended_mocks,
            **evidence,
        }
    finally:
        await s.browser.restore_mocks(suspended_mocks)
        # Best-effort: return the agent to where it was so record_bug isn't a
        # surprise navigation mid-flow, and refresh the element cache — the page
        # was reloaded twice even on a same-page (at_url omitted) check.
        try:
            if s.browser.has_page and s.browser.current_url != restore_url:
                await s.browser.goto(restore_url)
            s._last_elements = (await s.browser.get_state()).elements
        except Exception:
            pass

    if inconclusive is not None:
        return {
            "attempted": True,
            "reproduced": None,
            "at_url": nav_url,
            "reason": inconclusive,
            "target_text": target[:120],
            "expect": expect,
            "mocks_suspended": suspended_mocks,
            **evidence,
        }

    if expected_status is not None:
        hits = sum(1 for matched in observations if matched)
        receipt = {
            "attempted": True,
            "reproduced": bool(observations) and all(observations),
            "flaky": 0 < hits < len(observations),
            "runs": f"{hits}/{len(observations)}",
            "expect_status": expected_status,
            "observed_statuses": observed_statuses,
            "method": "fresh-load HTTP status re-check",
            "at_url": nav_url,
            **evidence,
        }
        if suspended_mocks:
            receipt["mocks_suspended"] = suspended_mocks
        return receipt

    receipt = _receipt_verdict(observations, expect)
    receipt.update(
        {
            "method": "fresh-load symptom re-check (server truth, no fixture reset)",
            "target_text": target[:120],
            "at_url": nav_url,
            "observed_present": observations,
            **evidence,
        }
    )
    if suspended_mocks:
        receipt["mocks_suspended"] = suspended_mocks
    return receipt


def _receipt_verdict(observations: list, expect: str) -> dict:
    """Pure verdict from a list of present/absent observations vs the claim.

    reproduced = every run matched the claim; flaky = some but not all did.
    """
    matches = [(p and expect == "present") or (not p and expect == "absent") for p in observations]
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
    try:
        actions = s.inputs.resolve(actions)
    except ValueError as exc:
        return {"attempted": False, "reason": str(exc)}
    expect = (verify.get("expect") or "").strip().lower()
    target = (verify.get("target_text") or verify.get("target") or "").strip()
    if expect not in ("present", "absent") or not target:
        return {
            "attempted": False,
            "reason": "replay receipt needs expect in {present, absent} and a non-empty target_text",
        }
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return {"attempted": False, "reason": "replay is only available in web mode"}
    if not actions:
        return {
            "attempted": False,
            "reason": "no recorded steps to replay — drive the bug via click_what/type_into/"
            "select_into/navigate so the action trace is captured",
        }

    start_url = next(
        (a.get("value") for a in actions if a.get("tool") == "navigate" and a.get("value")), None
    )
    start_url = _resolve_url(s, start_url) if start_url else (s.url or s.browser.current_url)
    try:
        res = await s.browser.replay(start_url, actions)
    except Exception as e:
        return {
            "attempted": True,
            "mode": "replay",
            "reproduced": None,
            "diverged": True,
            "steps": len(actions),
            "error": str(e)[:200],
            "at_url": start_url,
        }

    receipt = {
        "attempted": True,
        "mode": "replay",
        "steps": len(actions),
        "diverged": res["diverged"],
        "at_url": start_url,
        "writes_replayed": res.get("writes", 0),
        "target_text": target[:120],
        "expect": expect,
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
        receipt["reason"] = (
            "symptom already held before the journey — not attributable "
            "to these steps (pre-existing / residual state)"
        )
    else:
        receipt["reproduced"] = bool(held_after)

    # Optional minimization: narrow a confirmed reproduction to the minimal
    # sufficient steps — but ONLY for a write-free journey (re-running subsets
    # of a journey with writes would re-execute those writes many times).
    if verify.get("minimize") and receipt["reproduced"] is True and len(actions) > 1:
        if receipt.get("writes_replayed", 0) > 0:
            receipt["minimize_skipped"] = (
                "journey re-executes writes — minimizing would repeat them; skipped"
            )
        else:
            minimal = await _minimize_replay(s, expect, target, start_url, actions)
            receipt["minimal_count"] = len(minimal)
            receipt["minimal_steps"] = [
                (f"{a.get('tool')} {a.get('description') or ''}").strip() for a in minimal
            ]
    return receipt


async def _minimize_replay(
    s: "Session", expect: str, target: str, start_url: str, actions: list, max_replays: int = 40
) -> list:
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
            candidate = current[:i] + current[i + 1 :]
            if not candidate:
                continue
            budget -= 1
            if await _reproduces(candidate):
                current = candidate
                changed = True
                break
    return current
