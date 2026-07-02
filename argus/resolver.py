"""Resolve natural-language element descriptions to interactive elements.

Backend-agnostic: takes a list of InteractiveElement records and a
description like "Login button" or "the email field" and returns the
best match, an ambiguous shortlist, or nothing.

The agent loaded into Argus is expected to phrase its intent in human
language. We try to honour that intent without forcing the agent to
look up integer element indices. When intent is ambiguous, we surface
the top candidates so the agent can rephrase rather than misclick.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import InteractiveElement


@dataclass
class ResolveResult:
    """Outcome of trying to map a natural-language description to one element.

    Exactly one of these states is meaningful per call:
    - reason="unique": `found` is the element to use.
    - reason="ambiguous": `candidates` lists the top matches; agent must
       refine the description.
    - reason="no_match": no element scored above zero.
    - reason="no_elements": the page exposed nothing interactive.
    """
    found: Optional[InteractiveElement]
    candidates: List[Tuple[int, InteractiveElement]]  # (score, element)
    reason: str


# Words that describe element *kind* rather than identity. Used to apply
# a kind filter and stripped from the core description before scoring.
_KIND_HINTS = {
    "button": "button",
    "btn": "button",
    "link": "link",
    "anchor": "link",
    "field": "input",
    "input": "input",
    "box": "input",
    "textbox": "input",
    "textarea": "input",
    "checkbox": "checkbox",
    "radio": "radio",
    "dropdown": "select",
    "select": "select",
    "menu": "select",
}

# Filler words to drop from descriptions before scoring. Includes row-scoping
# scaffolding ("the Delete in the Buy-groceries row") and region scaffolding
# ("Register link in the navigation") — the tester's connective/location words,
# not content. A literal region label (a "Navigation" menu item) is still
# matched first by the exact-label fast path, so dropping these is safe.
_STOPWORDS = {
    "the", "a", "an", "this", "that", "in", "on", "of", "to",
    "row", "rows", "item", "entry", "near", "for", "with",
    "named", "labeled", "labelled", "label", "labels", "containing", "value", "whose",
    "navigation", "nav", "navbar", "header", "footer", "sidebar", "toolbar", "bar",
}

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def extract_ordinal(desc: str) -> Tuple[str, Optional[int]]:
    """Pull a positional selector out of a description, return (rest, n).

    Lets a tester pick among identical labels by position the way a human
    would: "Delete #2", "the 2nd Delete", "third Edit button", "last Delete".
    Returns the description with the ordinal removed and the index (1-based, or
    -1 for "last"), or (desc, None) when there's no ordinal.
    """
    d = desc.strip()
    m = re.search(r"#\s*(\d+)\s*$", d)
    if m:
        return d[:m.start()].strip(), int(m.group(1))
    m = re.search(r"\b(\d+)(?:st|nd|rd|th)\b", d, re.IGNORECASE)
    if m:
        return (d[:m.start()] + d[m.end():]).strip(), int(m.group(1))
    toks = d.split()
    if len(toks) > 1:  # a lone "second"/"last" is a label, not a position
        for i, t in enumerate(toks):
            low = t.lower()
            if low in _ORDINAL_WORDS:
                return " ".join(toks[:i] + toks[i + 1:]).strip(), _ORDINAL_WORDS[low]
            # "last X" -> from the end. The exact-label fast path runs before
            # this, so a real label like "Last name" is matched verbatim first.
            if low == "last":
                return " ".join(toks[:i] + toks[i + 1:]).strip(), -1
    return d, None


def kind_of(el: InteractiveElement) -> str:
    """Bucket an element into a coarse 'kind' category."""
    if el.tag == "a":
        return "link"
    if el.tag == "button":
        return "button"
    if el.tag in ("input", "textarea"):
        if el.type in ("checkbox", "radio"):
            return el.type
        if el.type in ("submit", "button"):
            return "button"
        return "input"
    if el.tag == "select":
        return "select"
    if el.role in ("button", "link", "tab", "menuitem"):
        return el.role
    return el.tag


def _strip_kind(desc: str) -> Tuple[List[str], Optional[str]]:
    """Lowercase + tokenise, remove the first kind-hint word; keep stopwords.

    Shared by split_description (which then drops stopwords) and the
    exact-label fast path (which must NOT drop them — 'in' is a real label
    word in 'Sign in', not filler)."""
    # "next to X" is row-scoping scaffolding, same as "near X" — strip the
    # PHRASE only (a standalone "Next" button must still resolve).
    desc = re.sub(r"\bnext\s+to\b", " ", desc.lower())
    # Underscores never appear in visible labels; an agent that types a
    # programmatic name it saw ("qty_1 input") should match the element whose
    # name we normalise the same way. (Hyphens are left alone — they occur in
    # real labels like "sign-in" / "e-mail".)
    desc = desc.replace("_", " ")
    # Strip surrounding quotes AND parens/brackets agents habitually wrap a label
    # or context in (`link "Tasks"`, `Title field (Buy groceries)`) — punctuation,
    # not part of the element's text, and left on they match nothing.
    raw = [w.strip('"“”‘’\'`()[]{}') for w in desc.strip().split()]
    raw = [w for w in raw if w]
    # A kind word can sit anywhere ("Submit button", "the Delete button for X"),
    # not just at the end. Consume EVERY kind word (first one is the signal) so a
    # second — "card input field", "dropdown menu" — doesn't leak into the core
    # and break matching. Strict callers can't fall back, so a residual kind word
    # there means a hard no_match.
    kind: Optional[str] = None
    kept: List[str] = []
    for w in raw:
        if w in _KIND_HINTS:
            if kind is None:
                kind = _KIND_HINTS[w]
            continue
        kept.append(w)
    return kept, kind


def split_description(desc: str) -> Tuple[str, Optional[str]]:
    """Strip stopwords and a trailing kind-hint, return (core, kind_hint)."""
    raw, kind = _strip_kind(desc)
    words = [w for w in raw if w not in _STOPWORDS]
    if not words:
        # Stopword filter ate everything — preserve the raw tokens. A
        # one-letter button labelled "A" or a heading "The" are real
        # labels that the agent will reasonably pass verbatim.
        words = raw
    return " ".join(words), kind


def _has_token(core: str, hay: str) -> bool:
    """Word-boundary containment: 'add' matches 'add task' but not 'address'.

    Bare substring matching let short verbs bleed into longer neighbours
    ('Add'->'Address', 'Edit'->'Credit') and still win as a confident unique
    pick. Require the needle to sit on word boundaries so only token-level
    hits count."""
    if not core or not hay:
        return False
    return re.search(r"(?<!\w)" + re.escape(core) + r"(?!\w)", hay) is not None


def _label_equals(el: InteractiveElement, phrase: str) -> bool:
    """True if a visible face of `el` equals `phrase` (whitespace-normalised)."""
    for face in (el.text, el.aria_label, el.placeholder, el.value):
        if face and " ".join(face.lower().split()) == phrase:
            return True
    return False


def _score(el: InteractiveElement, core: str) -> int:
    """Score an element 0-110 for how well it matches `core` (lowercased)."""
    if not core:
        return 0
    text = (el.text or "").lower().strip()
    aria = (el.aria_label or "").lower().strip()
    placeholder = (el.placeholder or "").lower().strip()
    # snake_case / kebab-case names are one token to a boundary scan ("qty_1",
    # "first_name"), so split the separators — lets "first name" match
    # name="first_name" and "qty" match name="qty_1". Internal identifiers, so
    # still low weight below.
    name = re.sub(r"[_\-]+", " ", (el.name or "").lower()).strip()
    id_ = re.sub(r"[_\-]+", " ", (el.id or "").lower()).strip()
    parent = (el.parent_context or "").lower().strip()
    href = (el.href or "").lower().strip()
    value = (el.value or "").lower().strip()

    # Exact equality first — these win decisively.
    if text == core:
        return 110
    if aria == core:
        return 100
    if placeholder == core:
        return 95
    if value == core:  # a pre-filled field's current value is what the user sees in it
        return 92
    if name == core:
        return 90
    if id_ == core:
        return 88

    score = 0
    if _has_token(core, text):
        score = max(score, 70 + min(20, len(core)))
    if _has_token(core, aria):
        score = max(score, 65 + min(15, len(core)))
    if _has_token(core, placeholder):
        score = max(score, 60 + min(15, len(core)))
    if _has_token(core, value):  # match a pre-filled input by its current value ("input with Alex")
        score = max(score, 58 + min(15, len(core)))
    # id/name are internal identifiers, not what a user sees. They earn a
    # token presence so a label-less form field is still reachable, but at
    # a weight that can never outrank a real visible/aria/placeholder hit.
    if _has_token(core, name):
        score = max(score, 22)
    if _has_token(core, id_):
        score = max(score, 20)
    # Links carry meaning in their href — testers target them by path
    # ("link to /product/1") or by a word that only lives in the URL
    # ("Wireless Headphones product link" — text is the name, "product" is in
    # the href). Low weight: a URL hint must never outrank a visible-text match.
    if _has_token(core, href):
        score = max(score, 24)

    # Word-set match: all core words present, tiered by WHERE they land.
    #  - all on the element's own face (text/aria/placeholder) -> strong.
    #  - some on the element's face + the rest in its row context (parent):
    #    a targeted row-scoped match. "Delete Buy groceries" puts "delete"
    #    on the button's own face and "buy groceries" in its row, so the
    #    Delete in the Buy-groceries row beats both its row-mates (Edit,
    #    whose face says "edit") and the same Delete in other rows (whose
    #    rows lack "buy groceries"). Reward the words the element itself
    #    carries so the right control in the right row wins.
    #  - only in internal attrs / row, nothing on the face -> barely there
    #    (Round 2 anti-leak: an id/parent substring must not pose as real).
    core_words = [w for w in core.split() if len(w) >= 2]
    if core_words:
        visible = " ".join([text, aria, placeholder, value])
        extended = " ".join([visible, name, id_, parent, href])
        if all(_has_token(w, visible) for w in core_words):
            score = max(score, 50)
        elif all(_has_token(w, extended) for w in core_words):
            own_hits = sum(1 for w in core_words if _has_token(w, visible))
            score = max(score, 44 + 8 * own_hits if own_hits else 22)

    if _has_token(core, parent):
        score = max(score, 30)

    return score


def _kind_compatible(el_kind: str, kind_filter: str) -> bool:
    """Is an element's kind acceptable under a kind filter?

    Submit/button-type inputs are already normalised to kind "button" by
    kind_of, so an exact match is all we need: a text input must not
    satisfy a "button" filter, nor a button an "input" filter.
    """
    return el_kind == kind_filter


def resolve_element(
    description: str,
    elements: List[InteractiveElement],
    kind_filter: Optional[str] = None,
    *,
    strict_kind: bool = False,
) -> ResolveResult:
    """Map a natural-language description to one interactive element.

    Args:
        description: Caller's words ("Login button", "the email field").
        elements: All interactive elements visible right now.
        kind_filter: Optional explicit kind override ("button", "input",
            "link", "select", "checkbox", "radio"). If omitted we infer
            from a trailing kind-hint word in `description`.
        strict_kind: When True, refuse to fall back to the full element
            pool if the kind filter eliminates everyone. Use this from
            type_into / select_into where falling back to a link or
            button gives the caller a confusing Playwright stack trace
            downstream. Default False matches click_what's prior
            "be helpful" behaviour.
    """
    if not elements:
        return ResolveResult(found=None, candidates=[], reason="no_elements")

    # Kind hint comes from the full description; the ordinal token (if any)
    # carries no kind word, so detect kind before stripping anything.
    raw_tokens, hinted_kind = _strip_kind(description)
    effective_kind = kind_filter or hinted_kind

    pool = elements
    if effective_kind:
        filtered = [el for el in elements if _kind_compatible(kind_of(el), effective_kind)]
        if strict_kind:
            pool = filtered  # may end up empty — that's the caller's choice
        else:
            pool = filtered or elements

    # Exact-label fast path. Match the full description (kind hint removed,
    # stopwords KEPT) against each element's visible label before stopword
    # stripping or ordinal extraction. A label that equals the description
    # verbatim is unambiguous, which fixes two bugs at once: stopword
    # stripping reducing 'Sign in' -> 'sign' (ties with 'Sign up'), and
    # extract_ordinal hijacking a literal 'Issue #42' as ordinal 42. A
    # literal label always beats a positional or stopword-stripped guess.
    phrase = " ".join(raw_tokens)
    if phrase:
        exact = [el for el in pool if _label_equals(el, phrase)]
        if len(exact) == 1:
            return ResolveResult(found=exact[0],
                                 candidates=[(110, exact[0])], reason="unique")

    description, ordinal = extract_ordinal(description)
    core, _ = split_description(description)

    # Kind-only call: the description was just a kind word ("dropdown",
    # "button", "the textbox") and after stripping it `core` is empty.
    # If the kind filter narrowed the pool to exactly one element, the
    # agent's intent is unambiguous — pick it.
    if not core and effective_kind and len(pool) == 1:
        return ResolveResult(found=pool[0], candidates=[(100, pool[0])], reason="unique")

    # Kind-only with MULTIPLE matches ("checkbox", "first checkbox", "last
    # radio"): scoring can't rank them (no label), but an ordinal makes it
    # deterministic. Without an ordinal it's genuinely ambiguous — list them so
    # the agent can say "first checkbox". (Before this, an empty core scored 0
    # for every element and the whole thing fell through to no_match.)
    if not core and effective_kind and len(pool) > 1:
        band = sorted(pool, key=lambda el: el.index)
        if ordinal == -1:
            return ResolveResult(found=band[-1], candidates=[(100, band[-1])], reason="unique")
        if ordinal is not None and 1 <= ordinal <= len(band):
            return ResolveResult(found=band[ordinal - 1], candidates=[(100, band[ordinal - 1])], reason="unique")
        return ResolveResult(found=None, candidates=[(90, el) for el in band[:5]], reason="ambiguous")

    scored: List[Tuple[int, InteractiveElement]] = []
    for el in pool:
        s = _score(el, core)
        if s > 0:
            scored.append((s, el))

    if not scored:
        # A soft kind hint matched NOTHING in its pool — likely a wrong hint
        # (agent called a link a "button" while real buttons filled the pool).
        # Retry across all elements without the kind word so a real match isn't
        # lost to a mislabel. Strict callers (type_into) never fall back — they
        # must not target the wrong kind. The retry drops the kind word (uses
        # `phrase`), so it can't re-narrow and recurse forever.
        if not strict_kind and effective_kind and pool is not elements and phrase:
            return resolve_element(phrase, elements, strict_kind=False)
        return ResolveResult(found=None, candidates=[], reason="no_match")

    scored.sort(key=lambda pair: -pair[0])
    top_score = scored[0][0]
    runner_up = scored[1][0] if len(scored) > 1 else 0

    # Positional pick: "Delete #2" resolves the base label, then selects the
    # Nth top-scoring match in document order. This is the deterministic
    # escape hatch for N identical controls (lists, tables) — exactly the
    # case where scoring alone can only report "ambiguous".
    if ordinal is not None:
        band = [el for sc, el in scored if sc == top_score]
        band.sort(key=lambda el: el.index)
        if ordinal == -1 and band:  # "last X" -> last in document order
            return ResolveResult(found=band[-1],
                                 candidates=[(top_score, band[-1])], reason="unique")
        if 1 <= ordinal <= len(band):
            return ResolveResult(found=band[ordinal - 1],
                                 candidates=[(top_score, band[ordinal - 1])], reason="unique")
        return ResolveResult(found=None, candidates=scored[:5], reason="ambiguous")

    if len(scored) == 1 or top_score >= runner_up + 15:
        return ResolveResult(found=scored[0][1], candidates=scored[:3], reason="unique")

    return ResolveResult(found=None, candidates=scored[:5], reason="ambiguous")


def describe(el: InteractiveElement) -> str:
    """Format an element for display in an ambiguous-resolution shortlist."""
    parts = [kind_of(el)]
    label = el.text or el.aria_label or el.placeholder or el.value or el.name
    if label:
        parts.append(f'"{label[:60]}"')
    if el.href:
        parts.append(f"-> {el.href[:60]}")
    if el.parent_context and el.parent_context.strip():
        ctx = el.parent_context.strip()[:50]
        if ctx and ctx not in (label or ""):
            parts.append(f"(near: {ctx!r})")
    return " ".join(parts)


# ── Screen-mode resolver ────────────────────────────────────────────
#
# ScreenElement (defined in argus.screen.backend) is shaped differently
# from InteractiveElement — it carries an AX role string, screen-coords,
# and a path of parent labels rather than DOM-style placeholder/href/etc.
# The scoring strategy is the same in spirit: match the description
# against text-like attributes, prefer interactive AX roles when a kind
# hint is given, fall back to the parent path for context. We keep the
# implementations parallel rather than trying to unify the two shapes —
# the difference between DOM and AX is a real difference, not noise.

# Map AX role strings to the resolver's coarse "kind" categories.
_AX_ROLE_TO_KIND = {
    "AXButton": "button",
    "AXLink": "link",
    "AXTextField": "input",
    "AXTextArea": "input",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "select",
    "AXComboBox": "select",
    "AXMenuItem": "menuitem",
    "AXMenuBarItem": "menuitem",
    "AXTab": "tab",
    "AXSlider": "slider",
    "AXStaticText": "text",
    "AXImage": "image",
}


def _ax_kind(role: str) -> str:
    return _AX_ROLE_TO_KIND.get(role, "")


def _score_screen(el, core: str) -> int:
    """Score a ScreenElement against a description core. Mirrors `_score`."""
    if not core:
        return 0
    title = (el.title or "").lower().strip()
    value = (el.value or "").lower().strip()
    desc = (el.description or "").lower().strip()
    role_desc = (el.role_description or "").lower().strip()
    # Path is a list of ancestor labels — useful for disambiguation.
    path_text = " ".join(p.lower() for p in (el.path or []) if p)

    if title == core or value == core:
        return 110
    if desc == core or role_desc == core:
        return 100

    score = 0
    for hay, base in ((title, 70), (value, 65), (desc, 60), (role_desc, 50)):
        if _has_token(core, hay):
            score = max(score, base + min(20, len(core)))

    # Word-set match, tiered by WHERE the words land (mirrors web _score):
    # all on the element's own face is strong; some on the face + the rest in
    # the ancestor path is a row-scoped match; path-only is barely there.
    visible = " ".join([title, value, desc, role_desc])
    extended = " ".join([visible, path_text])
    core_words = [w for w in core.split() if len(w) >= 2]
    if core_words:
        if all(_has_token(w, visible) for w in core_words):
            score = max(score, 50)
        elif all(_has_token(w, extended) for w in core_words):
            own_hits = sum(1 for w in core_words if _has_token(w, visible))
            score = max(score, 44 + 8 * own_hits if own_hits else 22)

    if _has_token(core, path_text):
        score = max(score, 30)

    return score


def _screen_label_equals(el, phrase: str) -> bool:
    """True if a visible face of a ScreenElement equals `phrase`."""
    for face in (el.title, el.value, el.description, el.role_description):
        if face and " ".join(face.lower().split()) == phrase:
            return True
    return False


def resolve_screen_element(
    description: str,
    elements: list,
    kind_filter: Optional[str] = None,
    *,
    strict_kind: bool = False,
):
    """Pick the screen element best matching `description`.

    Returns a ResolveResult with the same semantics as resolve_element
    but operating on ScreenElement records (from argus.screen.backend).
    Pass strict_kind=True from screen_type_into to refuse the cross-kind
    fallback (so we don't try to type into a button).
    """
    if not elements:
        return ResolveResult(found=None, candidates=[], reason="no_elements")

    # Kept at parity with web resolve_element: kind from the full description,
    # an exact-label fast path (stopwords kept), then ordinal + scoring.
    raw_tokens, hinted_kind = _strip_kind(description)
    effective_kind = kind_filter or hinted_kind

    pool = elements
    if effective_kind:
        filtered = [el for el in elements if _ax_kind(el.role) == effective_kind]
        if strict_kind:
            pool = filtered
        else:
            pool = filtered or elements

    phrase = " ".join(raw_tokens)
    if phrase:
        exact = [el for el in pool if _screen_label_equals(el, phrase)]
        if len(exact) == 1:
            return ResolveResult(found=exact[0],
                                 candidates=[(110, exact[0])], reason="unique")

    description, ordinal = extract_ordinal(description)
    core, _ = split_description(description)

    if not core and effective_kind and len(pool) == 1:
        return ResolveResult(found=pool[0], candidates=[(100, pool[0])], reason="unique")

    scored = []
    for el in pool:
        s = _score_screen(el, core)
        if s > 0:
            scored.append((s, el))

    if not scored:
        return ResolveResult(found=None, candidates=[], reason="no_match")

    scored.sort(key=lambda pair: -pair[0])
    top_score = scored[0][0]
    runner_up = scored[1][0] if len(scored) > 1 else 0

    # Positional pick among identical labels, scanned in reading order
    # (top-to-bottom, left-to-right) since screen elements carry coordinates.
    if ordinal is not None:
        band = [el for sc, el in scored if sc == top_score]
        band.sort(key=lambda el: (getattr(el, "y", 0), getattr(el, "x", 0)))
        if 1 <= ordinal <= len(band):
            return ResolveResult(found=band[ordinal - 1],
                                 candidates=[(top_score, band[ordinal - 1])], reason="unique")
        return ResolveResult(found=None, candidates=scored[:5], reason="ambiguous")

    if len(scored) == 1 or top_score >= runner_up + 15:
        return ResolveResult(found=scored[0][1], candidates=scored[:3], reason="unique")

    return ResolveResult(found=None, candidates=scored[:5], reason="ambiguous")


def describe_screen(el) -> str:
    """Render a ScreenElement for an ambiguous-resolution shortlist."""
    parts = [el.role]
    label = el.title or el.value or el.description or el.role_description
    if label:
        parts.append(f'"{label[:60]}"')
    parts.append(f"@ ({el.x},{el.y}) {el.width}x{el.height}")
    if el.path and len(el.path) > 1:
        ctx = " / ".join(p for p in el.path[-2:] if p)[:50]
        if ctx:
            parts.append(f"(in: {ctx})")
    return " ".join(parts)
