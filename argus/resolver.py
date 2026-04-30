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

# Filler words to drop from descriptions before scoring.
_STOPWORDS = {"the", "a", "an", "this", "that", "in", "on", "of"}


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


def split_description(desc: str) -> Tuple[str, Optional[str]]:
    """Strip stopwords and a trailing kind-hint, return (core, kind_hint)."""
    words = [w for w in desc.lower().strip().split() if w and w not in _STOPWORDS]
    kind: Optional[str] = None
    if words and words[-1] in _KIND_HINTS:
        kind = _KIND_HINTS[words[-1]]
        words = words[:-1]
    return " ".join(words), kind


def _score(el: InteractiveElement, core: str) -> int:
    """Score an element 0-110 for how well it matches `core` (lowercased)."""
    if not core:
        return 0
    text = (el.text or "").lower().strip()
    aria = (el.aria_label or "").lower().strip()
    placeholder = (el.placeholder or "").lower().strip()
    name = (el.name or "").lower().strip()
    id_ = (el.id or "").lower().strip()
    parent = (el.parent_context or "").lower().strip()

    # Exact equality first — these win decisively.
    if text == core:
        return 110
    if aria == core:
        return 100
    if placeholder == core:
        return 95
    if name == core:
        return 90
    if id_ == core:
        return 88

    score = 0
    if core in text:
        score = max(score, 70 + min(20, len(core)))
    if core in aria:
        score = max(score, 65 + min(15, len(core)))
    if core in placeholder:
        score = max(score, 60 + min(15, len(core)))
    if core in name:
        score = max(score, 55)
    if core in id_:
        score = max(score, 53)

    # Word-set match: all core words appear somewhere on the element.
    haystack = " ".join([text, aria, placeholder, name, id_, parent])
    core_words = [w for w in core.split() if len(w) >= 2]
    if core_words and all(w in haystack for w in core_words):
        score = max(score, 50)

    if core in parent:
        score = max(score, 30)

    return score


def _kind_compatible(el_kind: str, kind_filter: str) -> bool:
    """Is an element's kind acceptable under a kind filter?"""
    if el_kind == kind_filter:
        return True
    # Buttons and submit-like inputs can absorb each other.
    if {el_kind, kind_filter} <= {"button", "input"}:
        return False
    return False


def resolve_element(
    description: str,
    elements: List[InteractiveElement],
    kind_filter: Optional[str] = None,
) -> ResolveResult:
    """Map a natural-language description to one interactive element.

    Args:
        description: Caller's words ("Login button", "the email field").
        elements: All interactive elements visible right now.
        kind_filter: Optional explicit kind override ("button", "input",
            "link", "select", "checkbox", "radio"). If omitted we infer
            from a trailing kind-hint word in `description`.
    """
    if not elements:
        return ResolveResult(found=None, candidates=[], reason="no_elements")

    core, hinted_kind = split_description(description)
    effective_kind = kind_filter or hinted_kind

    pool = elements
    if effective_kind:
        filtered = [el for el in elements if _kind_compatible(kind_of(el), effective_kind)]
        # Fall back to full pool if the filter rules everyone out.
        pool = filtered or elements

    scored: List[Tuple[int, InteractiveElement]] = []
    for el in pool:
        s = _score(el, core)
        if s > 0:
            scored.append((s, el))

    if not scored:
        return ResolveResult(found=None, candidates=[], reason="no_match")

    scored.sort(key=lambda pair: -pair[0])
    top_score = scored[0][0]
    runner_up = scored[1][0] if len(scored) > 1 else 0

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
