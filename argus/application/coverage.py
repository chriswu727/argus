from __future__ import annotations

import asyncio
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from argus.domain.models import (
    PageState,
)

from .session import Session
from .text import _short

_COVERAGE_STATUSES = frozenset({"untested", "in_progress", "exercised", "blocked"})


_MAX_DISCOVERED_PAGES = 200


def _contract_items(label: str, values: Optional[List[str]]) -> tuple[List[str], Optional[str]]:
    if values is None:
        return [], None
    if len(values) > 20:
        return [], f"start_session: {label} accepts at most 20 items."
    cleaned = []
    seen = set()
    for value in values:
        text = " ".join(value.split())
        if not text:
            return [], f"start_session: {label} cannot contain an empty item."
        if len(text) > 300:
            return [], f"start_session: each {label} item must be at most 300 characters."
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(text)
    return cleaned, None


def _coverage_path(url: str) -> str:
    path = urlparse(url).path.rstrip("/") or "/"
    return path


def _coverage_reference_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("screen://"):
        return url
    return _coverage_path(url)


def _current_coverage_url(s: Session) -> str:
    if s.mode == "web" and s.browser is not None and s.browser.has_page:
        return s.browser.current_url
    if s.mode == "screen" and s.screen is not None:
        return f"screen://{s.screen.app_name or 'unknown'}"
    return s.url or ""


def _coverage_cursor(s: Session) -> dict:
    return {
        "actions": len(s.coverage_action_trace),
        "pages": len(s.pages_visited),
        "screenshots": len(s.screenshots),
        "verifications": len(s.verification_checks),
        "bugs": len(s.bugs),
        "observations": len(s.observations),
    }


def _coverage_evidence_refs(s: Session, cursor: dict) -> dict:
    urls = []
    candidates = list(s.pages_visited[cursor["pages"] :])
    current_url = _current_coverage_url(s)
    if current_url:
        candidates.append(current_url)
    for url in candidates:
        reference = _coverage_reference_url(url)
        if reference not in urls:
            urls.append(reference)

    action_candidates = s.coverage_action_trace[cursor["actions"] :]
    actions = []
    for action in action_candidates[:25]:
        tool = action.get("tool") or "action"
        description = action.get("description") or ""
        actions.append(
            {
                "tool": tool,
                "description": description,
            }
        )

    screenshot_candidates = s.screenshots[cursor["screenshots"] :]
    screenshots = [
        {
            "name": screenshot.name,
            "path": screenshot.path,
            "url": _coverage_reference_url(screenshot.url),
        }
        for screenshot in screenshot_candidates[:10]
    ]
    verification_candidates = s.verification_checks[cursor["verifications"] :]
    verifications = [dict(check) for check in verification_candidates[:10]]

    bug_candidates = s.bugs[cursor["bugs"] :]
    observation_candidates = s.observations[cursor["observations"] :]
    findings = []
    for bug in bug_candidates[:20]:
        receipt = bug.reproduction_receipt or {}
        findings.append(
            {
                "kind": "bug",
                "title": bug.title,
                "severity": bug.severity.value,
                "verified": receipt.get("reproduced") is True,
                "url": _coverage_reference_url(bug.url),
                "screenshot": bug.screenshot_path,
            }
        )
    remaining = 20 - len(findings)
    for observation in observation_candidates[:remaining]:
        findings.append(
            {
                "kind": "observation",
                "title": observation.title,
                "category": observation.category,
                "url": _coverage_reference_url(observation.url),
                "screenshot": observation.screenshot_path,
            }
        )

    return {
        "urls": urls[:20],
        "actions": actions,
        "screenshots": screenshots,
        "verifications": verifications,
        "findings": findings,
        "omitted": {
            "urls": max(0, len(urls) - 20),
            "actions": max(0, len(action_candidates) - 25),
            "screenshots": max(0, len(screenshot_candidates) - 10),
            "verifications": max(0, len(verification_candidates) - 10),
            "findings": max(0, len(bug_candidates) + len(observation_candidates) - 20),
        },
    }


def _update_coverage_from_state(s: Session, state: PageState) -> None:
    if not hasattr(s, "discovered_pages"):
        s.discovered_pages = []
    current = _coverage_path(state.url)
    if current not in s.discovered_pages:
        s.discovered_pages.append(current)
    for link in state.links or []:
        if len(s.discovered_pages) >= _MAX_DISCOVERED_PAGES:
            break
        if not link.get("isInternal"):
            continue
        path = _coverage_path(urljoin(state.url, link.get("href") or ""))
        if path not in s.discovered_pages:
            s.discovered_pages.append(path)


def _coverage_snapshot(s: Session, elapsed_seconds: Optional[float] = None) -> dict:
    goals = [dict(goal) for goal in getattr(s, "coverage_goals", [])]
    counts = {status: 0 for status in _COVERAGE_STATUSES}
    for goal in goals:
        status = goal.get("status", "untested")
        counts[status if status in counts else "untested"] += 1
    counts["total"] = len(goals)

    visited = []
    for url in getattr(s, "pages_visited", []):
        path = _coverage_path(url)
        if path not in visited:
            visited.append(path)
    discovered = list(getattr(s, "discovered_pages", []))
    for path in visited:
        if path not in discovered:
            discovered.append(path)

    if elapsed_seconds is None:
        started = getattr(s, "start_time", None)
        elapsed_seconds = (
            max(0.0, asyncio.get_event_loop().time() - started) if started is not None else 0.0
        )
    budget_minutes = max(0, int(getattr(s, "time_budget_minutes", 0) or 0))
    budget_seconds = budget_minutes * 60
    remaining = max(0.0, budget_seconds - elapsed_seconds) if budget_seconds else None

    return {
        "goals": goals,
        "summary": counts,
        "constraints": list(getattr(s, "constraints", [])),
        "pages": {
            "visited": visited,
            "discovered": discovered,
            "unvisited": [path for path in discovered if path not in visited],
        },
        "time_budget": {
            "minutes": budget_minutes,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "remaining_seconds": round(remaining, 1) if remaining is not None else None,
            "exceeded": bool(budget_seconds and elapsed_seconds > budget_seconds),
        },
    }


def _format_coverage(snapshot: dict) -> str:
    lines = []
    goals = snapshot["goals"]
    summary = snapshot["summary"]
    if goals:
        lines.append(
            f"Goals: {summary['exercised']}/{summary['total']} exercised, "
            f"{summary['blocked']} blocked, {summary['in_progress']} in progress, "
            f"{summary['untested']} untested"
        )
        pending = [goal["goal"] for goal in goals if goal["status"] in {"untested", "in_progress"}]
        if pending:
            shown = "; ".join(_short(goal, 70) for goal in pending[:4])
            lines.append(f"Still to cover: {shown}{' …' if len(pending) > 4 else ''}")
    unvisited = snapshot["pages"]["unvisited"]
    if unvisited:
        shown = ", ".join(unvisited[:6]) + (" …" if len(unvisited) > 6 else "")
        lines.append(
            f"Pages: {len(snapshot['pages']['visited'])}/{len(snapshot['pages']['discovered'])} visited; unvisited: {shown}"
        )
    budget = snapshot["time_budget"]
    if budget["minutes"]:
        elapsed = int(round(budget["elapsed_seconds"] / 60))
        if budget["exceeded"]:
            lines.append(f"Time: {elapsed}m elapsed; {budget['minutes']}m budget exceeded")
        else:
            remaining = max(0, int(round((budget["remaining_seconds"] or 0) / 60)))
            lines.append(f"Time: {elapsed}m elapsed; about {remaining}m remaining")
    return "\n".join(lines)


def _session_protocol(s: Session) -> str:
    lines = [
        "Session protocol (returned once; observe keeps the coverage ledger visible):",
        "1. MAP the supplied goals and the real user journeys that satisfy them.",
        "2. USE the product end-to-end with real state. Read post-action observations already returned; call observe only when evidence is missing or stale.",
        "3. HYPOTHESIZE user-affecting failures, including cross-page and persistence failures.",
        "4. VERIFY persistence-changing actions once with a fresh load; visible success feedback is not proof. Repeat only to resolve uncertainty.",
        "5. RECORD only reproducible bugs. Use record_observation for qualitative visual or UX evidence.",
        "6. COVER important goals and pages. Batch related goals in one coverage_update(goal=[...], status='in_progress') before their shared journey, then one update with observed evidence. Never mark untested goals exercised.",
        "Severity: HIGH for security, data loss, payment, or a blocked primary flow; MEDIUM for workflow friction, deceptive feedback, or inconsistency; LOW for minor UX.",
        "Authority: do not purchase, publish, delete real data, or cause another irreversible external effect unless explicitly authorized.",
    ]
    if s.coverage_goals:
        lines.append("Goals:")
        lines.extend(f"- {goal['goal']}" for goal in s.coverage_goals)
    else:
        lines.append(
            "Goals: infer a small set of important user journeys from the product and request."
        )
    if s.constraints:
        lines.append("User constraints (keep these visible in your decisions):")
        lines.extend(f"- {constraint}" for constraint in s.constraints)
    if s.time_budget_minutes:
        lines.append(
            f"Advisory time budget: {s.time_budget_minutes} minutes; finish safely and report uncovered work when it expires."
        )
    return "\n".join(lines)


def _coverage_goal_index(s: Session, query: str) -> tuple[Optional[int], Optional[str]]:
    normalized = " ".join(query.split()).casefold()
    if not normalized:
        return None, "coverage_update: goal cannot be empty."
    exact = [i for i, goal in enumerate(s.coverage_goals) if goal["goal"].casefold() == normalized]
    if exact:
        return exact[0], None
    matches = [
        i
        for i, goal in enumerate(s.coverage_goals)
        if normalized in goal["goal"].casefold() or goal["goal"].casefold() in normalized
    ]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        available = "; ".join(goal["goal"] for goal in s.coverage_goals)
        return None, f"coverage_update: no goal matches {query!r}. Available: {available}"
    candidates = "; ".join(s.coverage_goals[i]["goal"] for i in matches)
    return None, f"coverage_update: {query!r} is ambiguous. Candidates: {candidates}"
