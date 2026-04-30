"""Event capture for Argus.

Argus's job is to be the high-fidelity browser interface for a smart agent
acting as a senior human QA tester. The agent reads page state and decides
what is and isn't a bug — that is *not* this module's job.

This module exists for one narrow reason: console errors and HTTP-layer
network failures are *not visible* to the agent through page state extraction.
They surface as browser events (Page.console / Page.pageerror / Page.response).
We capture those events into Bug records so the agent has a structured handle
on them.

Everything else that used to live here (page-content regex, count consistency,
CSS state, SEO, a11y, performance, mixed content, dead-link aggregation,
state verification, toast/network cross-check) was removed in 0.5.0. A smart
agent reads page_text, computed styles, ARIA tree, and item lists directly
and reasons about them. Static rules duplicating that reasoning were
subtractive: maintenance burden, false positives, and crucially they pretended
Argus was the smart layer when in reality the agent is.

If you're reaching for this module to add a new detector, stop and ask
whether the agent could just *observe* the same thing and decide. Almost
always: yes.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List

from .models import Bug, BugType, Severity


class Detector:
    """Minimal event-to-Bug capture for the two channels the agent can't see directly."""

    def __init__(self) -> None:
        # Cross-call dedup so a single recurring console message doesn't
        # produce N copies of the same Bug across page navigations.
        self._seen: set[str] = set()

    def process_console_errors(
        self,
        errors: List[Dict],
        url: str,
        steps: List[str],
    ) -> List[Bug]:
        """Convert browser console events into deduplicated Bug records.

        Errors and exceptions are reported individually. Warnings differing
        only by URL (e.g. "preloaded resource X / Y / Z not used") are
        aggregated into a single Bug per pattern to avoid drowning the
        report in repetitive noise.
        """
        bugs: List[Bug] = []
        warnings = [e for e in errors if e["type"] == "warning"]
        non_warnings = [e for e in errors if e["type"] != "warning"]

        for err in non_warnings:
            key = f"console:{err['text'][:200]}"
            if key in self._seen:
                continue
            self._seen.add(key)
            severity = Severity.HIGH if err["type"] == "exception" else Severity.MEDIUM
            bugs.append(Bug(
                type=BugType.CONSOLE_ERROR,
                severity=severity,
                title=f"Console {err['type']}: {err['text'][:80]}",
                description=err["text"],
                url=url,
                steps_to_reproduce=list(steps),
                console_logs=[err["text"]],
                raw_error=err["text"],
            ))

        if warnings:
            url_re = re.compile(r"https?://\S+")
            pattern_counts: Counter = Counter()
            pattern_example: Dict[str, str] = {}
            for w in warnings:
                pattern = url_re.sub("[URL]", w["text"])[:80]
                pattern_counts[pattern] += 1
                if pattern not in pattern_example:
                    pattern_example[pattern] = w["text"]

            for pattern, count in pattern_counts.items():
                key = f"console_warn:{pattern}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                example = pattern_example[pattern]
                title = (
                    f"Console warning: {example[:80]}"
                    if count == 1
                    else f"{count} console warnings: {pattern[:70]}..."
                )
                bugs.append(Bug(
                    type=BugType.CONSOLE_ERROR,
                    severity=Severity.LOW,
                    title=title,
                    description=f"{count} warning(s). Example: {example[:200]}",
                    url=url,
                    steps_to_reproduce=list(steps),
                    console_logs=[example],
                ))

        return bugs

    def process_network_errors(
        self,
        errors: List[Dict],
        url: str,
        steps: List[str],
    ) -> List[Bug]:
        """Convert HTTP 4xx / 5xx responses into deduplicated Bug records."""
        bugs: List[Bug] = []
        for err in errors:
            key = f"network:{err['method']}:{err['url']}:{err['status']}"
            if key in self._seen:
                continue
            self._seen.add(key)
            severity = Severity.HIGH if err["status"] >= 500 else Severity.MEDIUM
            bugs.append(Bug(
                type=BugType.NETWORK_ERROR,
                severity=severity,
                title=f"HTTP {err['status']} — {err['method']} {err['url'][:60]}",
                description=f"{err['method']} {err['url']} returned {err['status']}",
                url=url,
                steps_to_reproduce=list(steps),
                network_logs=[err],
            ))
        return bugs
