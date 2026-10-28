from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from argus.application.context import current_runtime
from argus.domain.contracts import Rejection, ToolText
from argus.domain.findings import FindingEvidence
from argus.domain.models import (
    Bug,
    Observation,
)
from argus.domain.verification import VerificationSpec

from .coverage import _coverage_goal_index, _refresh_finding_coverage
from .events import _BUG_TYPE_BY_NAME, _SEVERITY_BY_NAME
from .evidence import _auto_screenshot
from .observation import _near_duplicate
from .progress import Stage
from .session import Session, _require_session
from .text import _marker_visible, _resolve_url, _short
from .verification import (
    _looks_logged_out,
)


async def record_bug(
    title: str,
    severity: str,
    evidence: FindingEvidence | str | None = None,
    verify: Optional[dict] = None,
    finding_id: str = "",
) -> str:
    """Record an observed, reproducible, user-affecting bug, not speculation or polish.

    Args:
        title: Specific one-line headline.
        severity: critical | high | medium | low | info. High: data loss,
            security, payment or blocked flow; medium: workflow friction or
            deceptive feedback; low: minor UX.
        evidence: Optional description, expected, actual, steps, goal (or goals),
            evidence_calls (caller-claimed call numbers, not independent proof),
            url, screenshot (auto, skip, label or approved image path), bug_type. Defaults:
            title as description, steps since the previous finding, current URL,
            automatic screenshot and ux_issue. Prefer state_verification for
            persistence failures, misleading_success for lying toasts,
            count_mismatch for counts, form_error for form failures, crash for
            dead pages. Other types: console_error, network_error, visual_anomaly,
            broken_link, text_anomaly, broken_image, seo_issue, accessibility,
            performance, mixed_content.
        verify: Optional independent fresh-load check:
            {"expect":"present|absent", "target_text":"...", "at_url":"/path"}.
            Absence needs the URL where the target should appear. The target
            must prove the claimed symptom, not a tangential fact; a text's
            presence alone cannot prove a count/logic inconsistency. HTTP failures
            use {"expect_status":404, "at_url":"/missing"} instead, never both.
            For persistence failures use verify_persistence's suggested_verify:
            fresh loads without repeating Save/Delete. clear_storage=true wipes
            client storage before checks, only when authorized; auth loss makes
            results inconclusive. Default reloads preserve cookies and storage.
            replay=true re-drives the recorded journey in a fresh context, then
            checks visible text. The symptom must hold after but not before;
            an unrecorded refresh is NOT replayed. Replay repeats real writes
            and requires authority. minimize=true only minimizes write-free
            confirmed journeys. Omit verify for visual/semantic judgments.
            No receipt means observed, not confirmed; inconclusive is not proof.
        finding_id: Recheck an existing session finding instead of adding one.
            Keep title/severity unchanged, supply verify and omit evidence.
            At most two rechecks; confirmed findings need no retry. Original
            receipts remain in verification_history. Do not repeat a journey
            just to attach a receipt.
    """
    s = _require_session()

    sev_key = (severity or "").strip().lower()
    if not title.strip():
        return Rejection("record_bug: title must not be empty.")
    if sev_key not in _SEVERITY_BY_NAME:
        return Rejection(
            f"record_bug: invalid severity {severity!r}. "
            f"Use one of: {', '.join(_SEVERITY_BY_NAME)}."
        )
    sev = _SEVERITY_BY_NAME[sev_key]
    if finding_id:
        return await _recheck_finding(s, finding_id, title, sev, evidence, verify)

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
    expected, actual = ev.get("expected", ""), ev.get("actual", "")
    if any(not isinstance(value, str) or len(value) > 4000 for value in (expected, actual)):
        return Rejection("record_bug: expected and actual must be text of at most 4000 characters.")
    coverage_goals, error = _finding_goals(s, ev)
    if error:
        return Rejection(error)
    claimed_calls = ev.get("evidence_calls", [])
    if (
        not isinstance(claimed_calls, list)
        or len(claimed_calls) > 50
        or any(type(value) is not int or value < 1 for value in claimed_calls)
    ):
        return Rejection("record_bug: evidence_calls accepts at most 50 positive call numbers.")
    if expected:
        description += f"\nExpected: {expected}"
    if actual:
        description += f"\nActual: {actual}"
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

    type_key = str(ev.get("bug_type") or "ux_issue").strip().lower()
    # The category is metadata; a guessed one ("inconsistency") must not cost
    # the finding itself. Keep it under the default type and say so.
    type_warning = ""
    if type_key not in _BUG_TYPE_BY_NAME:
        type_warning = (
            f"bug_type {type_key[:40]!r} is not recognized; recorded as ux_issue. "
            f"Known types: {', '.join(_BUG_TYPE_BY_NAME)}."
        )
        type_key = "ux_issue"
    bug_type = _BUG_TYPE_BY_NAME[type_key]

    # Keep the report tight — a repeat of an already-recorded finding (agents
    # re-log the same bug with slightly different wording) is dropped. Only
    # near-VERBATIM repeats are caught (high token overlap / same title), so two
    # genuinely distinct findings are never merged.
    dup = _near_duplicate(title, description, s.bugs)
    if dup is not None:
        return ToolText(
            f"Not recorded — near-duplicate of an already-logged finding: "
            f"{dup.title!r}. To attach a receipt, call record_bug with its finding_id, "
            "unchanged title/severity and verify; do not repeat the journey.",
            {
                "recorded": False,
                "reason": "duplicate",
                "duplicate_of": f"{s.run_id}:{s.bugs.index(dup) + 1}",
                "next_action": "Use finding_id with verify to recheck this finding (maximum two rechecks).",
            },
        )

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

    fresh_load = False
    if verify:
        try:
            fresh_load = (
                VerificationSpec.parse(verify).kind in {"text", "http_status"}
                and s.mode == "web"
                and s.browser is not None
                and s.browser.has_page
            )
        except ValueError:
            pass
    if fresh_load and not ev.get("url"):
        url = _resolve_url(s, verify.get("at_url") or verify.get("after_url") or url)

    screenshot_directive = ev.get("screenshot", "auto")
    screenshot_path: Optional[str] = None
    screenshot_label = None
    warnings = [type_warning] if type_warning else []
    if screenshot_directive != "skip":
        if screenshot_directive and (
            "/" in screenshot_directive or screenshot_directive.endswith(".png")
        ):
            runtime = current_runtime()
            screenshot_path = str(
                runtime.settings.access.check_file(
                    screenshot_directive, extra_roots=(runtime.settings.output_dir,)
                )
            )
        else:
            label = title if screenshot_directive in ("auto", "", None) else screenshot_directive
            screenshot_label = (
                "bug_" + "".join(c if c.isalnum() else "_" for c in label.lower())[:40]
            )
            if fresh_load:
                target_url = _resolve_url(
                    s, verify.get("at_url") or verify.get("after_url") or s.browser.current_url
                )
                if _resolve_url(s, url) != target_url:
                    warnings.append(
                        "Finding URL differs from verification target; screenshot omitted."
                    )
                    screenshot_label = None
            elif s.mode == "web" and s.browser and _resolve_url(s, url) != s.browser.current_url:
                warnings.append("Finding URL differs from current page; screenshot omitted.")
                screenshot_label = None
            if screenshot_label and not fresh_load:
                try:
                    screenshot_path = await _auto_screenshot(
                        s, screenshot_label, f"record_bug: {_short(title, 60)}"
                    )
                    if not screenshot_path:
                        warnings.append("Finding screenshot was unavailable.")
                except Exception:
                    warnings.append("Finding screenshot capture failed.")

    replay_slice = list(s.action_trace[s._actions_since_last_bug :])
    if verify:
        await current_runtime().progress(Stage.VERIFYING)
    receipt = (
        await current_runtime().verifiers.verify(
            s, verify, replay_slice, screenshot_label=screenshot_label if fresh_load else None
        )
        if verify
        else None
    )
    if receipt and fresh_load:
        if not ev.get("url"):
            url = receipt.get("at_url") or url
        landed = receipt.get("evidence_url")
        if screenshot_label and landed and _resolve_url(s, landed) != _resolve_url(s, url):
            warnings.append(
                f"Verification load ended at {_short(landed, 120)}; screenshot shows that page."
            )
        if screenshot_label:
            screenshot_path = receipt.get("screenshot_path")
            if not screenshot_path:
                warnings.append(
                    receipt.get("evidence_warning") or "Verification screenshot was unavailable."
                )

    description, receipt = await _guard_capsule(s, description, receipt)

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
        expected=expected,
        actual=actual,
        coverage_goals=coverage_goals,
        claimed_evidence_calls=list(claimed_calls),
        evidence_warnings=warnings,
        journey_end=len(s.action_trace),
    )
    s.bugs.append(bug)
    _refresh_finding_coverage(s, coverage_goals)
    verdict = (receipt or {}).get("reproduced")
    code = (
        "verified" if verdict is True else "not_reproduced" if verdict is False else "inconclusive"
    )
    runtime = current_runtime()
    if verify:
        await runtime.progress(
            Stage.VERIFIED
            if verdict is True
            else Stage.NOT_REPRODUCED
            if verdict is False
            else Stage.INCONCLUSIVE
        )
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
    out.extend(f"  evidence warning: {warning}" for warning in warnings)
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
                    f"  reproduction: INCONCLUSIVE — {receipt.get('reason', 'unknown cause')}. "
                    "Not a confirmation. For persistence failures, use a clean-load check "
                    "with this finding_id instead of repeating writes."
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
            'toast), recheck this finding_id with verify={"expect":"present|absent","target_text":'
            '"...","at_url":"/path"} so Argus re-confirms it from a clean load.'
        )
    out.append(f"  total bugs in session: {len(s.bugs)}")
    return ToolText(
        "\n".join(out),
        {
            "recorded": True,
            "finding_id": f"{s.run_id}:{len(s.bugs)}",
            "evidence_status": "observed" if receipt is None else code,
            "verdict": code,
            "receipt": receipt,
            "warnings": warnings,
            "next_action": (
                "Preserve this receipt; do not repeat the journey."
                if verdict is True
                else "If further evidence is needed, recheck this finding_id with verify. "
                "Prefer fresh loads for persistence failures; do not repeat writes to force confirmation."
            ),
            "finding": {
                "title": title,
                "severity": sev.value,
                "url": url,
                "screenshot": screenshot_path,
            },
        },
    )


def _finding_goals(s: Session, evidence: dict) -> tuple[list[str], str | None]:
    queries = evidence.get("goals", evidence.get("goal", []))
    if isinstance(queries, str):
        queries = [queries]
    if not isinstance(queries, list) or len(queries) > 20:
        return [], "record_bug: goal/goals accepts at most 20 declared goals."
    goals = []
    for query in queries:
        if not isinstance(query, str):
            return [], "record_bug: each goal must be text."
        index, error = _coverage_goal_index(s, query)
        if error:
            return [], error.replace("coverage_update:", "record_bug:", 1)
        goal = s.coverage_goals[index]["goal"]
        if goal not in goals:
            goals.append(goal)
    return goals, None


async def _guard_capsule(s: Session, description: str, receipt: dict | None) -> tuple:
    # A missing marker alone is not evidence of expiry on a legitimate page.
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
    return description, receipt


def _verdict_code(receipt: dict | None) -> str:
    if receipt is None:
        return "observed"
    verdict = receipt.get("reproduced")
    return (
        "verified" if verdict is True else "not_reproduced" if verdict is False else "inconclusive"
    )


async def _recheck_finding(
    s: Session, finding_id: str, title: str, severity, evidence, verify
) -> str:
    bug = next(
        (bug for index, bug in enumerate(s.bugs, 1) if finding_id == f"{s.run_id}:{index}"),
        None,
    )
    if bug is None:
        return Rejection("record_bug: finding_id must identify a finding in this session.")
    if title != bug.title or severity != bug.severity or evidence is not None:
        return Rejection("record_bug: rechecks keep title/severity unchanged and omit evidence.")
    if not isinstance(verify, dict) or not verify:
        return Rejection("record_bug: a recheck requires a non-empty verify clause.")
    previous = bug.reproduction_receipt
    if (previous or {}).get("reproduced") is True or len(bug.verification_history) >= 2:
        return ToolText(
            "Recheck not run: already verified or two rechecks exhausted. Preserve the evidence; "
            "do not repeat writes to force confirmation.",
            {
                "recorded": False,
                "updated": False,
                "finding_id": finding_id,
                "reason": "already_verified"
                if (previous or {}).get("reproduced") is True
                else "recheck_limit",
                "receipt": previous,
                "rechecks_remaining": 0,
            },
        )
    runtime = current_runtime()
    await runtime.progress(Stage.VERIFYING)
    # The slot is consumed up front: an interrupted replay may already have
    # repeated writes, so it must not become an unlimited retry.
    history = {
        "previous_receipt": previous,
        "verify": {key: value for key, value in verify.items() if not key.startswith("_")},
        "receipt": {
            "attempted": True,
            "reproduced": None,
            "reason": "recheck did not complete; it may have run partially",
        },
    }
    bug.verification_history.append(history)
    receipt = await runtime.verifiers.verify(s, verify, list(bug.replay_steps))
    bug.description, receipt = await _guard_capsule(s, bug.description, receipt)
    if isinstance(receipt, dict):
        receipt = {
            **receipt,
            "recheck": len(bug.verification_history),
            "previous_verdict": _verdict_code(previous),
        }
    history["receipt"] = receipt
    bug.reproduction_receipt = receipt
    _refresh_finding_coverage(s, bug.coverage_goals)
    verdict = (receipt or {}).get("reproduced")
    code = _verdict_code(receipt)
    await runtime.progress(
        Stage.VERIFIED
        if verdict is True
        else Stage.NOT_REPRODUCED
        if verdict is False
        else Stage.INCONCLUSIVE
    )
    runtime.events.publish("finding_rechecked", runtime.state.value, "record_bug", code)
    s.steps.append(f"record_bug recheck: {finding_id} ({code})")
    return ToolText(
        f"Rechecked {finding_id}: {code}. No new finding created.",
        {
            "recorded": False,
            "updated": True,
            "finding_id": finding_id,
            "evidence_status": code,
            "verdict": code,
            "receipt": receipt,
            "rechecks_remaining": 0 if verdict is True else 2 - len(bug.verification_history),
        },
    )


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
        return Rejection("record_observation: title and evidence are required.")
    # Like record_bug's bug_type, the category is metadata: a guessed one must
    # not cost the observation. Keep it under a general category and say so.
    warnings = []
    if category not in allowed_categories:
        warnings.append(
            f"category {category[:40]!r} is not recognized; recorded as usability. "
            f"Known categories: {', '.join(sorted(allowed_categories))}."
        )
        category = "usability"
    normalized = " ".join(title.lower().split())
    if any(" ".join(item.title.lower().split()) == normalized for item in s.observations):
        return ToolText(
            f"record_observation: duplicate title not recorded: {title!r}.",
            {"recorded": False, "reason": "duplicate"},
        )

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
    return ToolText(
        f"Recorded {category} observation: {title}\n"
        f"  screenshot: {screenshot_path or 'none'}\n"
        f"  total observations in session: {len(s.observations)}"
        + "".join(f"\n  warning: {warning}" for warning in warnings),
        {
            "recorded": True,
            "category": category,
            "title": title,
            "screenshot": screenshot_path,
            "finding_confirmed": False,
            "warnings": warnings,
        },
    )
