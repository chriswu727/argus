from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from argus.domain.models import (
    Bug,
    BugType,
    PageState,
)

from .session import Session


async def _capture_browser_events(
    s: Session, state: PageState, console_errs: list, network_errs: list
) -> list:
    """Capture browser-side events the agent cannot see directly.

    Console messages and HTTP-layer 4xx/5xx do not surface in page state —
    they only appear via Playwright event listeners. We turn those into Bug
    records so they show up in the session report. Everything else (page
    text, counts, CSS state, toasts) is the agent's job to interpret.
    """
    recent = s.steps[s._steps_since_last_bug :]
    bugs = s.detector.process_console_errors(console_errs, state.url, recent)
    bugs.extend(s.detector.process_network_errors(network_errs, state.url, recent))
    return bugs


_AUTO_CAPTURED_RECEIPT = {
    "attempted": False,
    "auto_captured": True,
    "reason": "console/network event captured by listener — not independently re-confirmed",
}


def _normalized_event_url(value: str) -> str:
    try:
        return urlparse(value or "")._replace(query="", fragment="").geturl().rstrip("/")
    except Exception:
        return (value or "").rstrip("/")


def _event_statuses(bug: Bug) -> set[int]:
    statuses = {int(log["status"]) for log in bug.network_logs if log.get("status") is not None}
    for raw in re.findall(r"\b[45]\d\d\b", f"{bug.title} {bug.description}"):
        statuses.add(int(raw))
    return statuses


def _attach_event_evidence(target: Bug, event: Bug) -> None:
    for line in event.console_logs:
        if line not in target.console_logs:
            target.console_logs.append(line)
    for log in event.network_logs:
        if log not in target.network_logs:
            target.network_logs.append(log)
    if not target.screenshot_path and event.screenshot_path:
        target.screenshot_path = event.screenshot_path


def _manual_target_for_event(existing: list[Bug], event: Bug) -> Optional[Bug]:
    manual = [bug for bug in existing if not (bug.reproduction_receipt or {}).get("auto_captured")]
    if event.type == BugType.NETWORK_ERROR:
        request_urls = {
            _normalized_event_url(log.get("url") or "")
            for log in event.network_logs
            if log.get("url")
        }
        matches = [bug for bug in manual if _normalized_event_url(bug.url) in request_urls]
        return matches[-1] if matches else None
    if event.type == BugType.CONSOLE_ERROR:
        source = _normalized_event_url(event.url)
        matches = [
            bug
            for bug in manual
            if _normalized_event_url(bug.url) == source
            and bug.type in {BugType.BROKEN_LINK, BugType.CRASH, BugType.NETWORK_ERROR}
        ]
        return matches[-1] if matches else None
    return None


def _file_event_bugs(s: "Session", new_bugs: list) -> list[Bug]:
    """File only new root causes and attach correlated event evidence."""
    existing = list(s.bugs)
    filed: list[Bug] = []
    network_targets: list[tuple[Bug, Bug]] = []

    for event in [bug for bug in new_bugs if bug.type == BugType.NETWORK_ERROR]:
        target = _manual_target_for_event(existing, event)
        if target is None:
            event.reproduction_receipt = event.reproduction_receipt or dict(_AUTO_CAPTURED_RECEIPT)
            s.bugs.append(event)
            filed.append(event)
            target = event
        else:
            _attach_event_evidence(target, event)
        network_targets.append((event, target))

    for event in [bug for bug in new_bugs if bug.type != BugType.NETWORK_ERROR]:
        target = _manual_target_for_event(existing, event)
        if target is None and event.type == BugType.CONSOLE_ERROR:
            source = _normalized_event_url(event.url)
            statuses = _event_statuses(event)
            candidates = {
                id(candidate): candidate
                for network_event, candidate in network_targets
                if _normalized_event_url(network_event.url) == source
                and statuses
                and statuses & _event_statuses(network_event)
            }
            if len(candidates) == 1:
                target = next(iter(candidates.values()))
        if target is None:
            event.reproduction_receipt = event.reproduction_receipt or dict(_AUTO_CAPTURED_RECEIPT)
            s.bugs.append(event)
            filed.append(event)
        else:
            _attach_event_evidence(target, event)

    return filed
