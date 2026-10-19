from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from argus.application.context import current_runtime
from argus.domain.models import (
    Bug,
    Observation,
)

from .events import _BUG_TYPE_BY_NAME, _SEVERITY_BY_NAME
from .evidence import _auto_screenshot
from .observation import _near_duplicate
from .session import _require_session
from .text import _marker_visible, _short
from .verification import (
    _looks_logged_out,
)


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
            IMPORTANT — the target must PROVE the symptom, not a nearby fact.
            target_text has to be the exact string whose presence/absence ALONE
            is the bug. For a COUNT or LOGIC inconsistency ("7 pending + 2 done
            != 8 total") no single text check establishes it — verifying that
            "8 total" merely EXISTS does not confirm the inconsistency and would
            stamp a misleading VERIFIED on tangential evidence. Record those as
            observation-based (omit verify), or verify the specific wrong value
            that should not be there.
            For a broken URL or API response whose HTTP status is the proof, use
            {"expect_status": 404, "at_url": "/missing"} instead of matching
            error-page copy. Status verification and text verification are
            alternatives and cannot be combined in one clause.
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
                Pick the SPECIFIC type, not the generic ux_issue: a "Saved!" /
                success toast that lied -> misleading_success; a delete or edit
                that did not persist -> state_verification; a wrong/inconsistent
                count or total -> count_mismatch; a JS exception or dead page ->
                crash; a form losing data / rejecting valid input -> form_error.
                Reserve ux_issue for genuine usability friction with no better
                fit — a data-loss or persistence bug labeled "ux_issue" reads as
                cosmetic next to its HIGH severity.
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
        raw = ev["steps"]
        if isinstance(raw, str):
            # A model very often passes steps as ONE string (a sentence or a
            # newline-joined list). list()-ing that explodes it into individual
            # CHARACTERS — one <li> per letter in the report. Split on lines, and
            # if it's a single line with INLINE numbering ("1. a 2. b 3. c"), split
            # on the number markers so each becomes its own step.
            parts = [ln.strip(" -*\t") for ln in raw.splitlines() if ln.strip()]
            if len(parts) <= 1:
                one = parts[0] if parts else raw
                inline = re.split(r"(?:^|\s+)\d+[.)]\s+", one)
                parts = [p for p in (x.strip() for x in inline) if p]
            # strip any leading "N." / "N)" left on a step so the <ol> supplies
            # the sole numbering (no "1. 1. ..." double-numbering).
            steps = [re.sub(r"^\s*\d+[.)]\s*", "", p) for p in parts] or [raw]
        elif isinstance(raw, (list, tuple)):
            steps = [str(x) for x in raw]
        else:
            steps = list(s.steps[s._steps_since_last_bug :])
    else:
        steps = list(s.steps[s._steps_since_last_bug :])
    if ev.get("url"):
        url = ev["url"]
    elif s.mode == "web" and s.browser is not None and s.browser.has_page:
        url = s.browser.current_url
    elif s.mode == "screen" and s.screen is not None and s.screen.app_name:
        url = f"screen://{s.screen.app_name}"
    else:
        url = ""

    type_key = (ev.get("bug_type") or "ux_issue").strip().lower()
    if type_key not in _BUG_TYPE_BY_NAME:
        return (
            f"record_bug: invalid bug_type {type_key!r}. "
            f"Use one of: {', '.join(_BUG_TYPE_BY_NAME)}."
        )
    bug_type = _BUG_TYPE_BY_NAME[type_key]

    # Keep the report tight — a repeat of an already-recorded finding (agents
    # re-log the same bug with slightly different wording) is dropped. Only
    # near-VERBATIM repeats are caught (high token overlap / same title), so two
    # genuinely distinct findings are never merged.
    dup = _near_duplicate(title, description, s.bugs)
    if dup is not None:
        return (
            f"Not recorded — near-duplicate of an already-logged finding: "
            f"{dup.title!r}. A tight report beats a repetitive one; sharpen the "
            f"angle or move on to a feature you haven't tested yet."
        )

    screenshot_directive = ev.get("screenshot", "auto")
    screenshot_path: Optional[str] = None
    if screenshot_directive == "skip":
        pass
    elif screenshot_directive in ("auto", "", None):
        label = "bug_" + "".join(c if c.isalnum() else "_" for c in title.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {_short(title, 60)}")
    elif "/" in screenshot_directive or screenshot_directive.endswith(".png"):
        # Treat as an existing path
        runtime = current_runtime()
        screenshot_path = str(
            runtime.settings.access.check_file(
                screenshot_directive, extra_roots=(runtime.settings.output_dir,)
            )
        )
    else:
        # Treat as a label for a fresh screenshot
        label = "".join(c if c.isalnum() else "_" for c in screenshot_directive.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {_short(title, 60)}")

    # Lower the barrier to engaging the receipt: if no explicit verify clause was
    # passed but the evidence dict carries a checkable target, build one from it.
    # (The agent still supplies the target — we never GUESS it, which would risk
    # a false VERIFIED; we only accept it from a second, more natural place.)
    if verify is None and (
        ev.get("target_text") or ev.get("target") or ev.get("expect_status") is not None
    ):
        verify = {
            "expect": (ev.get("expect") or "present"),
            "target_text": ev.get("target_text") or ev.get("target"),
            "at_url": ev.get("at_url") or ev.get("after_url") or "",
            "replay": bool(ev.get("replay")),
            "expect_status": ev.get("expect_status"),
        }
        if verify["expect_status"] is not None:
            verify.pop("expect")
            verify.pop("target_text")

    replay_slice = list(s.action_trace[s._actions_since_last_bug :])
    receipt = await current_runtime().verifiers.verify(s, verify, replay_slice) if verify else None

    # If a state capsule is in play, flag the finding ONLY when there's positive
    # evidence the session died — the marker is absent AND the page is a login
    # wall. (A marker simply not rendering on a legitimate authenticated page —
    # checkout, settings, a modal — must not falsely flag a real bug.) When it
    # does fire, also neutralize the receipt so an expired-session artifact can't
    # read as VERIFIED.
    if s._capsule_marker and s.mode == "web" and s.browser is not None and s.browser.has_page:
        try:
            cur = await s.browser.get_state()
            if not _marker_visible(s._capsule_marker, cur) and _looks_logged_out(cur):
                description = (
                    description.rstrip()
                    + "\n\n[UNRELIABLE: the restored state capsule's session appears expired "
                    "(login wall present, marker absent) — re-mint state and re-confirm.]"
                )
                if isinstance(receipt, dict) and receipt.get("attempted"):
                    receipt = {
                        **receipt,
                        "reproduced": None,
                        "reason": "recorded against an expired capsule session",
                    }
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
    verdict = (receipt or {}).get("reproduced")
    code = (
        "verified" if verdict is True else "not_reproduced" if verdict is False else "inconclusive"
    )
    runtime = current_runtime()
    runtime.events.publish("finding_recorded", runtime.state.value, "record_bug", code)
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
                out.append(
                    f"  reproduction: CONFIRMED by replaying {receipt.get('steps')} step(s) "
                    f"from a cold start ({receipt['expect']} {receipt['target_text']!r})"
                )
                if receipt.get("minimal_count") is not None and receipt[
                    "minimal_count"
                ] < receipt.get("steps", 0):
                    out.append(
                        f"  minimal repro: {receipt['minimal_count']} of {receipt['steps']} step(s) "
                        f"suffice — {receipt.get('minimal_steps')}"
                    )
                elif receipt.get("minimize_skipped"):
                    out.append(f"  minimize: {receipt['minimize_skipped']}")
            else:
                if receipt.get("expect_status") is not None:
                    out.append(
                        f"  reproduction: CONFIRMED {receipt['runs']} from clean load "
                        f"(HTTP {receipt['expect_status']} @ {receipt['at_url']})"
                    )
                else:
                    out.append(
                        f"  reproduction: CONFIRMED {receipt['runs']} from clean load "
                        f"({receipt['expect']} {receipt['target_text']!r} @ {receipt['at_url']})"
                    )
        elif receipt.get("reproduced") is False:
            if is_replay:
                out.append(
                    f"  reproduction: NOT REPRODUCED — replayed {receipt.get('steps')} step(s) "
                    "but the symptom was absent. Re-check before trusting this."
                )
            else:
                tag = "FLAKY" if receipt.get("flaky") else "NOT REPRODUCED"
                if receipt.get("expect_status") is not None:
                    out.append(
                        f"  reproduction: {tag} {receipt['runs']} on clean reload — "
                        f"expected HTTP {receipt['expect_status']}, observed "
                        f"{receipt.get('observed_statuses', [])}."
                    )
                else:
                    out.append(
                        f"  reproduction: {tag} {receipt['runs']} on clean reload — "
                        "the symptom you reported did not hold up. Re-check before trusting this."
                    )
        else:  # None — inconclusive
            if is_replay and receipt.get("diverged"):
                out.append(
                    "  reproduction: INCONCLUSIVE — replay path diverged (a recorded step no "
                    "longer resolves), so the journey couldn't be re-driven. Not a confirmation."
                )
            elif is_replay:
                out.append(
                    "  reproduction: INCONCLUSIVE — the symptom already held before the journey, "
                    "so it can't be attributed to these steps. Not a confirmation."
                )
            elif receipt.get("reason"):
                out.append(f"  reproduction: INCONCLUSIVE — {receipt['reason']}")
            else:
                out.append(f"  reproduction: check errored — {receipt.get('error', 'unknown')}")
    else:
        # No receipt: nudge toward one for the (common) text-checkable case so
        # the precision moat actually engages — a weaker agent won't otherwise.
        out.append(
            "  reproduction: NONE — this finding is unverified (your say-so). If the "
            "symptom is text-checkable (an item present/absent, a wrong count, a lying "
            'toast), re-record with verify={"expect":"present|absent","target_text":'
            '"...","at_url":"/path"} so Argus re-confirms it from a clean load.'
        )
    out.append(f"  total bugs in session: {len(s.bugs)}")
    return "\n".join(out)


async def record_observation(
    title: str,
    evidence: str,
    category: str = "visual",
    screenshot: str = "auto",
) -> str:
    """Record a qualitative review note without classifying it as a bug.

    Use this for visual polish, hierarchy, readability, content, responsive,
    or usability observations that are useful evidence but do not meet the
    reproducible user-affecting bug bar.

    Args:
        title: Short, specific observation headline.
        evidence: What is visible and why it matters.
        category: visual, usability, content, responsive, or accessibility.
        screenshot: "auto", "skip", an existing image path, or a screenshot label.
    """
    s = _require_session()
    title = (title or "").strip()
    evidence = (evidence or "").strip()
    category = (category or "visual").strip().lower()
    allowed_categories = {"visual", "usability", "content", "responsive", "accessibility"}

    if not title or not evidence:
        return "record_observation: title and evidence are required."
    if category not in allowed_categories:
        return "record_observation: category must be one of: " + ", ".join(
            sorted(allowed_categories)
        )
    normalized = " ".join(title.lower().split())
    if any(" ".join(item.title.lower().split()) == normalized for item in s.observations):
        return f"record_observation: duplicate title not recorded: {title!r}."

    if s.mode == "web" and s.browser is not None and s.browser.has_page:
        url = s.browser.current_url
    elif s.mode == "screen" and s.screen is not None:
        url = f"screen://{s.screen.app_name or 'unknown'}"
    else:
        url = s.url or ""

    screenshot_path: Optional[str] = None
    if screenshot == "skip":
        pass
    elif screenshot in ("auto", "", None):
        label = (
            "observation_" + "".join(char if char.isalnum() else "_" for char in title.lower())[:40]
        )
        screenshot_path = await _auto_screenshot(
            s, label, f"record_observation: {_short(title, 60)}"
        )
    elif Path(screenshot).expanduser().exists():
        runtime = current_runtime()
        screenshot_path = str(
            runtime.settings.access.check_file(
                screenshot, extra_roots=(runtime.settings.output_dir,)
            )
        )
    else:
        label = "".join(char if char.isalnum() else "_" for char in screenshot.lower())[:40]
        screenshot_path = await _auto_screenshot(
            s, label or "observation", f"record_observation: {_short(title, 60)}"
        )

    s.observations.append(
        Observation(
            title=title,
            evidence=evidence,
            url=url,
            category=category,
            screenshot_path=screenshot_path,
        )
    )
    return (
        f"Recorded {category} observation: {title}\n"
        f"  screenshot: {screenshot_path or 'none'}\n"
        f"  total observations in session: {len(s.observations)}"
    )
