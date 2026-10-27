from __future__ import annotations

import asyncio
from typing import Optional

from argus.differ import compute_changes
from argus.domain.contracts import ErrorCode, Rejection, ToolText
from argus.domain.models import (
    InteractiveElement,
)
from argus.resolver import (
    kind_of,
    resolve_element,
)

from .capture import _capture_browser_events, _file_event_bugs
from .evidence import _auto_screenshot, _record_action, _record_coverage_action
from .expectations import _evaluate_expectation, _reconcile_action
from .inspection import _visual_inspection_query
from .observation import _begin_dialogs, _dialog_lines, _finish_dialogs, _resolve_or_error
from .payloads import observation_data
from .session import _require_session


async def test_action(
    target: str,
    expectation: str = "",
    expect: Optional[dict] = None,
    double_click: bool = False,
    accept_dialog: bool = False,
) -> str:
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
        double_click: Double-click instead of click (inline editors).
        accept_dialog: Accept a JS confirm/prompt this click raises; dialogs
            are otherwise dismissed and reported. Only when the user authorized
            the action the dialog confirms.
    """
    s = _require_session()

    # A double-click may target exact visible text (an inline-edit label),
    # the same as click_what; otherwise resolve an interactive element.
    text_target = None
    if double_click and s.mode == "web" and s.browser is not None:
        query, _ = _visual_inspection_query(target)
        if await s.browser.count_exact_text(query) == 1:
            text_target = query
    el = None
    if text_target is None:
        el, err = _resolve_or_error(s, target)
        if err:
            return err
        label = el.text or el.aria_label or el.placeholder or el.name or el.tag
    else:
        label = text_target
    step = f'test_action({target!r}) -> "{label[:60]}"'
    if expectation:
        step += f" — expected: {expectation}"
    s.steps.append(step)

    s.browser.drain_errors()
    before = await s.browser.get_state()
    net_pre = s.browser.network_cursor()

    try:
        # Route through _locator so a duplicate-label target hits the resolved
        # element, not the first DOM match (same fix as click_what).
        mark = _begin_dialogs(s, accept_dialog)
        try:
            if text_target is not None:
                if not await s.browser.double_click_text(text_target):
                    raise RuntimeError("double-click failed")
            elif double_click:
                idx = s._last_elements.index(el)
                if not await s.browser.double_click(idx, s._last_elements):
                    raise RuntimeError("double-click failed")
            else:
                await s.browser.click_resolved(s._last_elements.index(el), s._last_elements)
            await s.browser.settle()
        finally:
            dialogs = _finish_dialogs(s, mark)
    except Exception as exc:
        ss = await _auto_screenshot(s, "action_failed", step)
        return Rejection(
            f"test_action({target!r}) — click failed: {exc}\n"
            f"The element may be obscured, stale, or removed. "
            f"Screenshot: {ss}",
            ErrorCode.BACKEND,
        )

    await asyncio.sleep(0.3)
    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)
    if text_target is not None:
        _record_action(s, "double_click", target, f"text:{text_target}")
    else:
        _record_action(s, "double_click" if double_click else "click_what", target)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _capture_browser_events(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, expectation or target)

    ss_path = await _auto_screenshot(s, f"action_{label[:20]}", step)
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    filed_bugs = _file_event_bugs(s, new_bugs)

    lines = [
        f'ACTION: {"Double-clicked" if double_click else "Clicked"} "{label[:60]}" '
        f"via target {target!r}{_dialog_lines(dialogs)}",
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

    if filed_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(filed_bugs)} new):")
        for bug in filed_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")
    if len(new_bugs) > len(filed_bugs):
        lines.append(
            f"\nAttached {len(new_bugs) - len(filed_bugs)} browser event(s) "
            "to an existing root-cause finding."
        )

    new_reqs = s.browser.network_events_since(net_pre)
    xs_evidence, xs_check = _reconcile_action(new_reqs, before, after)
    if new_reqs or xs_check:
        lines.append("")
        lines.append("CROSS-STACK (UI claim vs wire):")
        for e in xs_evidence:
            lines.append(f"  {e}")
        if xs_check:
            lines.append(f"  CHECK: {xs_check}")

    checks = []
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
    return ToolText(
        "\n".join(lines),
        {
            "observation": observation_data(s, after),
            "changes": changes[:100],
            "changes_omitted": max(0, len(changes) - 100),
            "expectations": [
                {"predicate": label, "matches": matches, "detail": detail}
                for label, matches, detail in checks
            ],
            "screenshot": ss_path,
            "dialogs": dialogs,
            "auto_captured_findings": len(filed_bugs),
            "finding_confirmed": False,
        },
    )


def _checked_value(value) -> bool | None:
    """Read a checkbox value given as a bool or as on/off-style text."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "on", "1", "checked", "check", "x"}:
        return True
    if text in {"false", "no", "off", "0", "unchecked", "uncheck", ""}:
        return False
    return None


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
    partial = False
    for field_desc, value in form_fields.items():
        # Re-observe before each field; SPA re-renders shift element refs.
        state = await s.browser.get_state()
        s._last_elements = state.elements

        result = resolve_element(field_desc, state.elements, kind_filter="input")
        if result.reason != "unique" or result.found is None:
            # Try without the kind filter — sometimes selects or buttons are intended
            result_any = resolve_element(field_desc, state.elements)
            if result_any.reason != "unique" or result_any.found is None:
                field_results.append(f"[MISS] {field_desc!r} — {result.reason}")
                partial = True
                continue
            el = result_any.found
        else:
            el = result.found

        selector = s.browser.selector_for(el)
        try:
            if kind_of(el) in ("checkbox", "radio", "switch"):
                wanted = _checked_value(value)
                if wanted is None:
                    raise ValueError(f"{kind_of(el)} takes true or false, not {value!r}")
                if el.checked != wanted:
                    await s.browser.set_checked_resolved(
                        state.elements.index(el), state.elements, wanted
                    )
                # Record the target state, not a click: a replay can start with
                # the box already checked (persisted storage), and a click would
                # then undo it.
                _record_action(s, "set_checked", field_desc, "true" if wanted else "false")
                field_results.append(
                    f"[OK] {field_desc!r} set to {'checked' if wanted else 'unchecked'}"
                )
                s.steps.append(f"Set {field_desc!r} to {'checked' if wanted else 'unchecked'}")
                continue
            if el.tag == "select":
                await s.browser.select_selector(selector, str(value), timeout=5000)
            else:
                await s.browser.fill_selector(selector, str(value), timeout=5000)
            field_results.append(f"[OK] {field_desc!r} (value omitted)")
            s.steps.append(f"Fill {field_desc!r} (value omitted)")
            _record_action(
                s, "select_into" if el.tag == "select" else "type_into", field_desc, str(value)
            )
        except Exception as exc:
            field_results.append(f"[FAIL] {field_desc!r} — {exc}")
            partial = True

    # Submit
    state = await s.browser.get_state()
    s._last_elements = state.elements
    submit_label = ""

    if submit == "enter":
        # Press Enter on whatever element is currently focused, falling back
        # to the last filled field if we can determine it.
        try:
            await s.browser.keyboard_press("Enter")
            await s.browser.settle()
            submit_label = "Enter key"
            s.steps.append("Press Enter to submit")
            _record_action(s, "press_key", "Enter")
        except Exception as exc:
            await _auto_screenshot(s, "form_no_submit", "Enter-key submit failed")
            return Rejection(
                f"FORM: filled {len(field_results)} fields but Enter-submit failed: {exc}",
                ErrorCode.BACKEND,
            )
    else:
        if submit == "auto":
            # Auto-discover: prefer a button whose text matches typical submit verbs.
            submit_keywords = [
                "submit",
                "save",
                "create",
                "sign in",
                "log in",
                "login",
                "register",
                "send",
                "add",
                "continue",
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
                return Rejection(
                    f"FORM: filled {len(field_results)} fields but submit "
                    f"description {submit!r} did not resolve: {result.reason}.\n"
                    + "\n".join(field_results)
                )
            target_el = result.found

        if target_el is None:
            await _auto_screenshot(s, "form_no_submit", "Could not find submit button")
            return Rejection(
                f"FORM: filled {len(field_results)} fields but no submit button "
                f'found. Pass submit="enter" or submit="<description>" '
                f"explicitly.\n" + "\n".join(field_results)
            )

        submit_label = target_el.text or target_el.aria_label or "submit"
        s.steps.append(f"Click {submit_label!r}")
        sel = s.browser.selector_for(target_el)
        try:
            await s.browser.click_selector(sel, timeout=5000)
            await s.browser.settle()
            _record_action(s, "click_what", submit_label)
        except Exception as exc:
            await _auto_screenshot(s, "form_submit_fail", "Submit click failed")
            return Rejection(
                f"FORM: filled {len(field_results)} fields but submit click failed: {exc}\n"
                + "\n".join(field_results),
                ErrorCode.BACKEND,
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
    _record_coverage_action(s, "test_form", f"{len(form_fields)} field(s) via {submit_label}")
    ss_path = await _auto_screenshot(s, "form_result", f"Form: {submit_label}")
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    filed_bugs = _file_event_bugs(s, new_bugs)

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
        f"  URL: {before_url} -> {after.url} {'(redirected)' if redirected else '(same page)'}"
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

    if filed_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(filed_bugs)} new):")
        for bug in filed_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")
    if len(new_bugs) > len(filed_bugs):
        lines.append(
            f"\nAttached {len(new_bugs) - len(filed_bugs)} browser event(s) "
            "to an existing root-cause finding."
        )

    lines.append(
        "\nDecide: was the outcome what you expected? If the form accepted "
        "garbage / lost data / showed a misleading toast / etc., call record_bug."
    )
    lines.append(f"Screenshot: {ss_path}")
    return ToolText(
        "\n".join(lines),
        {
            "observation": observation_data(s, after),
            "fields": field_results,
            "changes": changes[:100],
            "changes_omitted": max(0, len(changes) - 100),
            "screenshot": ss_path,
            "finding_confirmed": False,
            "partial": partial,
        },
    )
