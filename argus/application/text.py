from __future__ import annotations

from urllib.parse import urljoin

from argus.domain.models import (
    PageState,
)
from argus.domain.verdict import frames_present
from argus.domain.verdict import token_present as _token_present

from .session import Session


def _resolve_url(s: "Session", url: str) -> str:
    """Resolve a possibly-relative URL against the current page.

    Playwright's goto rejects bare paths like "/tasks"; agents (and our own
    docstrings) routinely pass them. Join against the live page origin.
    """
    if url.startswith(("http://", "https://")):
        return url
    base = ""
    if s.browser is not None and s.browser.has_page:
        base = s.browser.current_url
    if not url:
        return base
    return urljoin(base, url) if base else url


_NOT_TEXT_FIELDS = {
    "hidden",
    "password",
    "checkbox",
    "radio",
    "file",
    "image",
    "submit",
    "button",
    "reset",
    "range",
    "color",
}


def _state_frames(state: PageState) -> list[dict]:
    """Per-frame facts for the verdict rules, main frame first.

    A browser-extracted state carries them; a state built without the
    collector falls back to its main-document text and extracted controls.
    """
    if state.frames:
        return state.frames
    buttons = []
    fields = []
    for el in state.elements:
        if el.frame:
            continue
        if el.tag == "button" or el.role == "button" or el.type in {"submit", "button"}:
            buttons.extend(filter(None, (el.text, el.value, el.aria_label)))
        elif el.tag in {"input", "textarea", "select"} and el.value:
            if (el.type or "text") not in _NOT_TEXT_FIELDS:
                fields.append(el.value)
    return [
        {
            "text": state.searchable_text or "",
            "fields": fields,
            "headings": [str(heading.get("text", "")) for heading in state.headings],
            "buttons": buttons,
            "password": any(el.type == "password" and not el.frame for el in state.elements),
            "truncated": bool(state.text_truncated),
        }
    ]


def _main_facts(state: PageState) -> dict:
    return _state_frames(state)[0]


def _text_in_state(text: str, state: PageState) -> bool:
    """The target is in some frame's visible text or a visible field's value."""
    if not text.strip():
        return False
    return frames_present(text, _state_frames(state))


def _marker_visible(text: str, state: PageState) -> bool:
    """Like _text_in_state but VISIBLE text only — never matches an input's
    `value`. A liveness marker (a user's name/email) is commonly pre-filled into
    a logged-out login form's value, which would falsely read as 'logged in'.
    """
    if not text.strip():
        return False
    if _token_present(text, state.searchable_text):
        return True
    for el in state.elements:
        if el.text and _token_present(text, el.text):
            return True
    return False


def _visible_text_in_state(text: str, state: PageState) -> bool:
    """Visible text of any frame — but NOT field values (the replay just typed
    into those; matching them would certify a symptom off the text the replay
    itself entered)."""
    if not text.strip() or state is None:
        return False
    return any(_token_present(text, frame.get("text") or "") for frame in _state_frames(state))


def _short(text: str, n: int = 60) -> str:
    """Truncate to a WORD boundary with an ellipsis (never mid-word like [:60],
    which produced timeline labels ending in 'persiste' / 'Pendi')."""
    import textwrap

    t = " ".join((text or "").split())
    return textwrap.shorten(t, width=n, placeholder="…") if t else ""


async def _present_with_scroll(
    s: "Session", target: str, initial_state, visible_only: bool = False, max_scrolls: int = 20
) -> bool:
    """Presence check robust to VIRTUALIZED / infinite / below-the-fold lists.

    A virtualization window (react-window/virtuoso) only renders the rows near
    the viewport, so a row scrolled out of view is NOT in the DOM and reads as
    absent — which would let the receipt false-certify a deletion of a row that
    is actually still there. If the target isn't in the initial state, scroll
    down (bounded) and re-check; stop at the page bottom, when scrolling stalls,
    or after max_scrolls so infinite scroll can't run away."""
    check = _visible_text_in_state if visible_only else _text_in_state
    if check(target, initial_state):
        return True
    last_y = None
    for _ in range(max_scrolls):
        await s.browser.scroll_by(1000)
        state = await s.browser.get_state()
        if check(target, state):
            return True
        vp = state.viewport or {}
        if vp.get("atBottom"):
            break
        y = vp.get("scrollY")
        if y is not None and y == last_y:
            break  # scroll position not advancing — nothing more to reveal
        last_y = y
    return False
