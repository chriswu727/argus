from __future__ import annotations

import re
from typing import List, Optional

from argus.application.context import current_runtime
from argus.domain.contracts import ErrorCode, Rejection, ToolText
from argus.domain.models import (
    Bug,
    InteractiveElement,
    PageState,
)
from argus.resolver import (
    describe as describe_element,
)
from argus.resolver import (
    kind_of,
    resolve_element,
)
from argus.security import _redact

from .coverage import (
    _apply_coverage_updates,
    _coverage_goal_index,
    _coverage_snapshot,
    _format_coverage,
    _update_coverage_from_state,
)
from .payloads import observation_data
from .progress import Stage
from .screen import _observe_screen
from .session import Session, _require_session


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

    if getattr(state, "canvases", None):
        lines.append("")
        lines.append(
            "Canvas regions (pixel-drawn — NOT in the DOM above; you are partly blind here):"
        )
        for c in state.canvases:
            tag = f" {c['label']!r}" if c.get("label") else (f" #{c['id']}" if c.get("id") else "")
            lines.append(
                f"  canvas{tag} {c.get('w')}x{c.get('h')} at ({c.get('left')},{c.get('top')}), center ({c.get('x')},{c.get('y')})"
            )
        lines.append(
            "  -> A canvas draws its UI as pixels (charts, maps, whiteboards, PDF, games). "
            "observe() can't read it. Call screenshot() to SEE it, then act with coordinates: "
            "click_at(x,y) / type_at(x,y,text) / hover_at(x,y)."
        )

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
                bits.append(f"{key}={v[:40]!r}")
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
            lines.append("  Note: more content below the fold — scroll_down to reveal it.")

    lines.append("")
    lines.append(
        'To interact: click_what("description"), type_into("field", "text"), '
        'select_into("dropdown", "value"). For deeper inspection of one element '
        "(computed styles / outerHTML / truncation), use inspect_element."
    )
    return "\n".join(lines)


def _near_duplicate(title: str, description: str, bugs) -> Optional["Bug"]:
    """Return an already-recorded bug this one near-verbatim repeats, else None.
    Conservative on purpose: an exact (normalized) title match, or ≥0.8 token
    Jaccard over title+description. High threshold so distinct findings that
    merely share vocabulary are never merged away (that would cost recall)."""

    def toks(t: str, d: str) -> set:
        return set(re.findall(r"[a-z0-9]+", f"{t} {d}".lower()))

    new_title = " ".join((title or "").lower().split())
    new = toks(title or "", description or "")
    if not new:
        return None
    for b in bugs:
        if " ".join((b.title or "").lower().split()) == new_title:
            return b
        old = toks(b.title or "", b.description or "")
        if old and len(new & old) / len(new | old) >= 0.8:
            return b
    return None


def _toast_line(toasts) -> str:
    """Surface toasts the moment a click produces them. Toasts auto-dismiss in
    seconds, so an agent that calls observe() a beat later misses them — yet the
    lying "Saved!"/"Created!" toast is the single most common deception bug. Show
    it now, framed as a claim to verify, not proof."""
    if not toasts:
        return ""
    shown = " | ".join(f'"{t[:80]}"' for t in toasts[:3])
    return (
        f"\nToast shown now: {shown}\n"
        "(A toast is a CLAIM, not proof — if it says saved/created/deleted/updated, "
        "verify the data actually persisted on a fresh load before trusting it.)"
    )


_STATE_CHANGE_VERBS = (
    "delete",
    "remove",
    "save",
    "add ",
    "create",
    "submit",
    "update",
    "toggle",
    "apply",
    "confirm",
    "publish",
    "post ",
    "send",
    "checkout",
    "pay",
    "complete",
    "archive",
    "mark ",
    "sign up",
)


def _verify_nudge(label: str, had_toast: bool) -> str:
    """After a state-changing click, nudge the agent to verify_persistence. The
    persistence-lie bugs (fake delete, off-by-one count, lying 'Saved' toast) are
    the SIGNATURE class the reproduction receipt exists for, but agents chronically
    trust the UI's success feedback instead of re-loading to confirm — so recall of
    exactly the bugs Argus is best at drops. Skip if a toast already carried the
    same warning (avoid double-nagging)."""
    lo = f" {(label or '').lower().strip()} "
    if not had_toast and any(v in lo for v in _STATE_CHANGE_VERBS):
        return (
            "\n  Persistence check: that was a state-changing action — the UI response "
            "is a CLAIM, not proof. Call verify_persistence(expect=..., target_text=...) "
            "on a fresh load; fake deletes, lying 'Saved' toasts, and off-by-one counts hide here."
        )
    return ""


def _tab_count(s) -> int:
    try:
        return len(s.browser.page_urls())
    except Exception:
        return 0


def _new_tab_line(s, before_count: int) -> str:
    """A click that opened a NEW tab (target=_blank, window.open, OAuth/payment
    popup) otherwise gives no signal — the driver stays on the old page, so the
    agent keeps observing it and the new-tab flow goes untested. Surface it so the
    agent can switch and test it (the exact multi-tab flows scripted E2E skips)."""
    try:
        pages = s.browser.page_urls()
    except Exception:
        return ""
    if len(pages) <= before_count:
        return ""
    idx = len(pages) - 1
    try:
        url = pages[idx] or "(blank)"
    except Exception:
        url = "(unknown)"
    return (
        f"\n  A new tab opened (tab {idx}): {url[:90]} — the click spawned a second "
        f"tab (OAuth / popup / open-in-new-tab). You are still on the original tab; "
        f"call tabs_switch({idx}) to inspect and test it."
    )


_EDGE_CASES = {
    "number": "negative, zero, a huge value (overflow), decimals, and non-numeric text",
    "email": "no @, a space, a trailing dot, unicode, and a very long value",
    "tel": "letters, a too-short number, spaces/dashes, and a leading +",
    "url": "no scheme, a space, javascript:, and a very long value",
    "date": "an impossible date (2024-13-40), a far-future/past year, and free text",
    "password": "empty, one character, a very long value, and whitespace-only",
    "search": "empty, whitespace-only, a DIFFERENT case than the stored data, and special chars",
    "textarea": "empty, whitespace-only, a very long value, and HTML/script content",
    "text": "empty, whitespace-only, a very long value, special chars <>&\"'/, and unicode/emoji",
}


def _edge_case_hint(s, el) -> str:
    """A tester tries the same handful of edge cases on each field TYPE — empty,
    boundary, wrong-type, special chars — where validation bugs hide (whitespace
    accepted, no max length, unbounded numbers, case-sensitive search). Agents type
    ONE valid value and move on, so those go unfound. Surface the type's checklist
    ONCE per field-type per session (opt-in judgment, not a nag)."""
    ftype = (getattr(el, "type", None) or "").lower()
    key = (
        ftype
        if ftype in _EDGE_CASES
        else ("textarea" if getattr(el, "tag", "") == "textarea" else "text")
    )
    seen = getattr(s, "_edge_hinted", None)
    if seen is None:
        seen = set()
        s._edge_hinted = seen
    if key in seen:
        return ""
    seen.add(key)
    return (
        f"\n  Edge cases a tester probes on this {key} field (not just one valid "
        f"value): {_EDGE_CASES[key]}. Submit each and watch for input accepted when "
        f"it should be rejected."
    )


def _begin_dialogs(s, accept_dialog: bool):
    """Mark the dialog log and, when asked, accept the next dialog only."""
    token = s.browser.accept_next_dialog() if accept_dialog else None
    return s.browser.dialog_cursor(), token


def _finish_dialogs(s, mark) -> list:
    """Dialogs the action raised; drops an accept that no dialog consumed."""
    cursor, token = mark
    if token is not None:
        s.browser.discard_dialog_response(token)
    return [
        {
            "type": item.get("type"),
            "message": _redact(str(item.get("message") or ""))[:200],
            "response": item.get("responded_with"),
        }
        for item in s.browser.dialogs_since(cursor)
    ]


def _dialog_lines(dialogs: list) -> str:
    """A confirm() that was dismissed silently looks like a dead button."""
    lines = []
    for item in dialogs:
        response = "accepted" if item["response"] == "accept" else "dismissed"
        line = f"\nDIALOG: {item['type']} {item['message']!r} was {response}."
        if item["type"] in ("confirm", "prompt") and response == "dismissed":
            line += " If the user authorized this action, repeat it with accept_dialog=true."
        lines.append(line)
    return "".join(lines)


def _new_events_line(s, before_console: int, before_network: int) -> str:
    """One-line surface of console/network errors that fired since the given
    counts — a PEEK, not a drain (get_errors still records them as findings).
    A backend 500 or a JS error triggered by an action never shows on the page,
    so bring it to the agent's attention the moment its action caused it, instead
    of relying on a get_errors call it may never make."""
    b = getattr(s, "browser", None)
    if b is None:
        return ""
    if hasattr(b, "errors_since"):
        ce, ne = b.errors_since(before_console, before_network)
    else:
        ce = b.console_errors[before_console:]
        ne = b.network_errors[before_network:]
    if not ne and not ce:
        return ""
    bits = []
    if ne:
        r = ne[0]
        what = (
            f"HTTP {r['status']}"
            if r.get("status") is not None
            else f"FAILED ({r.get('failure', 'no response')})"
        )
        bits.append(
            f"{len(ne)} network error(s) — e.g. {what} "
            f"{r.get('method', '')} {_redact(r.get('url') or '')[:60]}"
        )
    if ce:
        bits.append(f"{len(ce)} console error(s) — e.g. {(ce[0].get('text') or '')[:70]}")
    return (
        "\nThis action fired (invisible on the page): "
        + "; ".join(bits)
        + ". Call get_errors to capture it as a finding."
    )


def _count_delta_note(prev_counts, prev_url, state) -> str:
    """Surface a change in on-page counts since the last observe of the SAME url.
    Off-by-one / miscount bugs (add 1 item, the total jumps 2; delete 1, count
    unchanged) hide here, and the agent rarely uses test_action's diff — so bring
    the delta to plain observe(). Same-url only, so navigation isn't noise."""
    if prev_counts is None or prev_url != state.url:
        return ""
    deltas = [
        f"{label!r}: {prev_counts[label]} -> {val}"
        for label, val in state.counts.items()
        if label in prev_counts and prev_counts[label] != val
    ]
    if not deltas:
        return ""
    return (
        "\n\nCount change since your last look here (does it match what you just "
        "did? off-by-one and miscounts hide here):\n  " + "\n  ".join(deltas)
    )


def _nearest_labels(description: str, elements, k: int = 4) -> List[str]:
    """The closest on-page elements to a failed description, by token overlap.
    Turns a bare 'no element matches' into a 'did you mean' shortlist so the
    agent self-corrects (or realizes the element isn't here) without spending a
    whole observe round-trip — a common way real agents stall."""

    def tokens(text: str) -> set:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    desc_toks = tokens(description)
    if not desc_toks:
        return []
    scored = []
    for el in elements:
        # A checkbox's value ("on") is not a label; describe() hides it too.
        value = None if (el.type or "").lower() in ("checkbox", "radio") else el.value
        label = el.text or el.aria_label or el.placeholder or value or el.name or ""
        own = len(desc_toks & tokens(label))
        # An unlabeled control (a row checkbox) is only nameable by its row, so
        # its row counts in full; for a labeled control the row is a weak hint.
        row = len(desc_toks & (tokens(el.parent_context or "") - tokens(label)))
        if not own and not row:
            continue
        score = 2 * own + (row if not label or own else row / 2)
        score += 0.5 if kind_of(el) in desc_toks else 0
        scored.append((score, describe_element(el)))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:k]]


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
        description,
        s._last_elements,
        kind_filter=kind_filter,
        strict_kind=strict_kind,
    )

    if result.reason == "unique" and result.found is not None:
        return result.found, None

    if result.reason == "no_elements":
        return None, Rejection(
            "No interactive elements visible. Call observe() first, or scroll_down "
            "if you expect content below the fold."
        )

    if result.reason == "no_match":
        near = _nearest_labels(description, s._last_elements)
        hint = ("\nClosest on the page:\n" + "\n".join(f"  {d}" for d in near)) if near else ""
        return None, Rejection(
            f"No element matches {description!r}. Pass a target description from "
            f"observe() as-is, or describe the element differently.{hint}"
        )

    # ambiguous
    lines = [f"Description {description!r} is ambiguous — multiple matches:"]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_element(el)}")
    lines.append("")
    lines.append(
        "Pick a more specific description (mention surrounding text, kind hint "
        "like 'button' / 'field', or which section of the page)."
    )
    return None, Rejection("\n".join(lines))


def _coverage_line(s: Session, state: PageState) -> str:
    try:
        _update_coverage_from_state(s, state)
        text = _format_coverage(_coverage_snapshot(s))
        return f"\n\nCoverage:\n{text}" if text else ""
    except Exception:
        return ""


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
    await current_runtime().progress(Stage.OBSERVING)
    if s.mode != "web" or s.browser is None:
        if s.mode != "screen" or s.screen is None:
            return Rejection(
                "observe: the active session has no usable backend.", ErrorCode.BACKEND
            )
        return await _observe_screen(s)
    if not s.browser.has_page:
        return Rejection(
            "observe: no open page — all tabs were closed. Call "
            "navigate(url) or start_session(url) to recover."
        )
    state = await s.browser.get_state()
    previous_counts = getattr(s, "_last_observed_counts", None)
    previous_url = getattr(s, "_last_observed_url", None)
    delta_note = _count_delta_note(
        getattr(s, "_last_observed_counts", None), getattr(s, "_last_observed_url", None), state
    )
    s._last_observed_counts = dict(state.counts)
    s._last_observed_url = state.url
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)
    text = _format_observation(state) + delta_note + _coverage_line(s, state)
    return ToolText(text, observation_data(s, state, previous_counts, previous_url))


async def coverage_update(goal: str | list[str], status: str, evidence: str = "") -> str:
    """Update one goal or a batch of related goals with shared evidence.

    Args:
        goal: Goal text, a unique part of it, or 1-20 related goals.
        status: untested, in_progress, exercised, or blocked.
        evidence: Concrete observed result; required for exercised and blocked.
    """
    s = _require_session()
    error = _apply_coverage_updates(s, [{"goal": goal, "status": status, "evidence": evidence}])
    if error:
        return Rejection(error)
    status = status.strip().lower()
    queries = [goal] if isinstance(goal, str) else goal
    indices = [_coverage_goal_index(s, query)[0] for query in queries]
    snapshot = _coverage_snapshot(s)
    names = "; ".join(s.coverage_goals[index]["goal"] for index in indices)
    result = f"Coverage updated: {names} → {status}.\n" + _format_coverage(snapshot)
    for index in indices:
        refs = s.coverage_goals[index].get("evidence_refs")
        if refs:
            result += (
                "\nEvidence linked: "
                f"{len(refs['urls'])} URL(s), {len(refs['actions'])} action(s), "
                f"{len(refs['screenshots'])} screenshot(s), "
                f"{len(refs['verifications'])} verification(s), "
                f"{len(refs['findings'])} finding(s)."
            )
    await current_runtime().progress(Stage.COVERAGE)
    return ToolText(result, {"coverage": snapshot})
