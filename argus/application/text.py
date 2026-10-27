from __future__ import annotations

import re
from urllib.parse import urljoin

from argus.domain.models import (
    PageState,
)

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
    # A boundary matters only on a word-character edge of the needle: "-50%"
    # printed straight after a price ("$89.99-50%") is a real occurrence.
    before = r"(?<!\w)" if re.match(r"\w", needle) else ""
    after = r"(?!\w)" if re.search(r"\w$", needle) else ""
    return re.search(before + re.escape(needle) + after, haystack) is not None


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
