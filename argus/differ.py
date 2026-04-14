"""State diff engine — compares two PageStates and reports what changed."""
from __future__ import annotations

import re
from typing import List

from .models import PageState


def compute_changes(
    before: PageState,
    after: PageState,
    action_description: str = "",
) -> List[str]:
    """Compare two PageStates and return human-readable change lines."""
    changes = []

    # URL change
    if before.url != after.url:
        changes.append(f"URL: {before.url} -> {after.url} (navigated)")

    # Title change
    if before.title != after.title:
        changes.append(f"Title: '{before.title}' -> '{after.title}'")

    # Element count
    before_count = len(before.elements)
    after_count = len(after.elements)
    if before_count != after_count:
        diff = after_count - before_count
        sign = "+" if diff > 0 else ""
        changes.append(f"Elements: {before_count} -> {after_count} ({sign}{diff})")

    # Elements added/removed (by text, capped at 5)
    before_texts = {(e.tag, e.text) for e in before.elements if e.text}
    after_texts = {(e.tag, e.text) for e in after.elements if e.text}
    removed = before_texts - after_texts
    added = after_texts - before_texts
    for tag, text in list(removed)[:5]:
        changes.append(f"Removed: <{tag}> '{text[:50]}'")
    for tag, text in list(added)[:5]:
        changes.append(f"Added: <{tag}> '{text[:50]}'")

    # Toast messages (new)
    before_toasts = set(before.toast_messages)
    after_toasts = set(after.toast_messages)
    new_toasts = after_toasts - before_toasts
    for toast in new_toasts:
        changes.append(f"Toast appeared: '{toast[:100]}'")

    # Item list changes
    for key in set(list(before.item_lists.keys()) + list(after.item_lists.keys())):
        b_items = before.item_lists.get(key, [])
        a_items = after.item_lists.get(key, [])
        if len(b_items) != len(a_items):
            changes.append(f"List '{key[:30]}': {len(b_items)} items -> {len(a_items)} items")

    # Count changes
    for label in set(list(before.counts.keys()) + list(after.counts.keys())):
        b_val = before.counts.get(label)
        a_val = after.counts.get(label)
        if b_val != a_val:
            changes.append(f"Count '{label}': {b_val} -> {a_val}")

    # Targeted text check based on action_description
    if action_description:
        # Extract quoted strings or key nouns from description
        targets = re.findall(r'"([^"]+)"', action_description)
        targets += re.findall(r"'([^']+)'", action_description)
        # Also try the last few words as a target
        words = action_description.split()
        if len(words) >= 2 and not targets:
            targets.append(" ".join(words[-2:]))

        for target in targets[:3]:
            was_present = target.lower() in before.page_text.lower()
            is_present = target.lower() in after.page_text.lower()
            if was_present and not is_present:
                changes.append(f"Text '{target}': was present -> GONE")
            elif not was_present and is_present:
                changes.append(f"Text '{target}': was absent -> NOW PRESENT")

    if not changes:
        changes.append("No visible changes detected")

    return changes
