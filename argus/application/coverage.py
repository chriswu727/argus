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
        "url": _current_coverage_url(s),
    }


def _coverage_evidence_refs(
    s: Session, cursor: dict, end: dict | None = None, goal: str = ""
) -> dict:
    end = end if end is not None else _coverage_cursor(s)
    urls = []
    action_candidates = s.coverage_action_trace[cursor["actions"] : end["actions"]]
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

    bug_candidates = [
        bug
        for index, bug in enumerate(s.bugs)
        if (goal and goal in bug.coverage_goals)
        or (cursor["bugs"] <= index < end["bugs"] and not bug.coverage_goals)
    ]
    linked_screenshots = {bug.screenshot_path for bug in bug_candidates if bug.screenshot_path}
    unrelated_screenshots = {
        bug.screenshot_path
        for bug in s.bugs
        if bug.coverage_goals and goal not in bug.coverage_goals
    }
    screenshot_candidates = [
        shot
        for index, shot in enumerate(s.screenshots)
        if (
            cursor["screenshots"] <= index < end["screenshots"]
            and shot.path not in unrelated_screenshots
        )
        or shot.path in linked_screenshots
    ]
    screenshots = [
        {
            "name": screenshot.name,
            "path": screenshot.path,
            "url": _coverage_reference_url(screenshot.url),
        }
        for screenshot in screenshot_candidates[:10]
    ]
    verification_candidates = s.verification_checks[cursor["verifications"] : end["verifications"]]
    verifications = [dict(check) for check in verification_candidates[:10]]

    observation_candidates = s.observations[cursor["observations"] : end["observations"]]
    candidates = (
        list(s.pages_visited[cursor["pages"] : end["pages"]])
        + [action.get("url", "") for action in action_candidates]
        + [shot.url for shot in screenshot_candidates]
        + [check.get("at_url", "") for check in verification_candidates]
        + [bug.url for bug in bug_candidates]
        + [observation.url for observation in observation_candidates]
        + [end.get("url", "")]
    )
    for url in candidates:
        reference = _coverage_reference_url(url)
        if reference and reference not in urls:
            urls.append(reference)
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


def _refresh_finding_coverage(s: Session, goals: list[str]) -> None:
    for index, selected in enumerate(s.coverage_goals):
        if selected["goal"] in goals and "evidence_refs" in selected:
            selected["evidence_refs"] = _coverage_evidence_refs(
                s, s._coverage_goal_cursors[index], s._coverage_goal_ends[index], selected["goal"]
            )


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
        "6. COVER important goals and pages. Batch related goals in coverage_update(goal=[...], status='in_progress') before their shared journey. Submit final exercised/blocked updates with observed evidence in end_session(coverage_updates=[...]); use coverage_update mid-session. Never mark untested goals exercised.",
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


def _apply_coverage_updates(s: Session, updates: list[dict], *, final: bool = False) -> str | None:
    if not isinstance(updates, list) or len(updates) > 20:
        return "coverage_update: provide at most 20 updates."
    if not updates:
        return None
    if not s.coverage_goals:
        return "coverage_update: this session has no supplied goals. Continue exploring and report pages visited."
    changes = []
    seen = set()
    for update in updates:
        if not isinstance(update, dict) or set(update) - {"goal", "status", "evidence"}:
            return "coverage_update: each update accepts only goal, status and evidence."
        status = update.get("status")
        if not isinstance(status, str) or status.strip().lower() not in _COVERAGE_STATUSES:
            return "coverage_update: status must be untested, in_progress, exercised, or blocked."
        status = status.strip().lower()
        if final and status not in {"exercised", "blocked"}:
            return "coverage_update: final updates must be exercised or blocked."
        evidence = update.get("evidence", "")
        if not isinstance(evidence, str):
            return "coverage_update: evidence must be text."
        evidence = " ".join(evidence.split())
        if len(evidence) > 1000:
            return "coverage_update: evidence must be at most 1000 characters."
        if status in {"exercised", "blocked"} and not evidence:
            return f"coverage_update: {status} requires concrete evidence."
        goal = update.get("goal")
        queries = [goal] if isinstance(goal, str) else goal
        if not isinstance(queries, list) or not 1 <= len(queries) <= 20:
            return "coverage_update: provide 1-20 goals."
        for query in queries:
            if not isinstance(query, str):
                return "coverage_update: each goal must be text."
            index, error = _coverage_goal_index(s, query)
            if error:
                return error
            if index in seen:
                return "coverage_update: goals must resolve to distinct entries."
            seen.add(index)
            changes.append((index, status, evidence))

    cursor = _coverage_cursor(s)
    cursors = dict(s._coverage_goal_cursors)
    ends = dict(s._coverage_goal_ends)
    frozen = set(s._coverage_goal_frozen)
    touched = {index for index, _, _ in changes}
    focused = {index for index, status, _ in changes if status == "in_progress"}
    other_work = any(status != "untested" for _, status, _ in changes)
    for index in cursors:
        if index in touched:
            continue
        # A new group closes every other window; other work closes finalized ones.
        finalized = s.coverage_goals[index].get("status") in {"exercised", "blocked"}
        if focused or (other_work and finalized):
            ends.setdefault(index, dict(cursor))
            frozen.add(index)
    planned = []
    for index, status, evidence in changes:
        selected = dict(s.coverage_goals[index])
        selected.update(status=status, evidence="" if status == "untested" else evidence)
        if status == "untested":
            selected.pop("evidence_refs", None)
            cursors.pop(index, None)
            ends.pop(index, None)
            frozen.discard(index)
        elif status == "in_progress":
            selected.pop("evidence_refs", None)
            cursors[index] = dict(cursor)
            ends.pop(index, None)
            frozen.discard(index)
        else:
            start = cursors.setdefault(index, dict(s._coverage_evidence_cursor))
            # Re-finalizing extends the window while no other goal has taken over.
            end = ends[index] if index in frozen else dict(cursor)
            ends[index] = end
            selected["evidence_refs"] = _coverage_evidence_refs(s, start, end, selected["goal"])
        planned.append((index, selected))

    for index, selected in planned:
        s.coverage_goals[index].clear()
        s.coverage_goals[index].update(selected)
    s._coverage_goal_cursors = cursors
    s._coverage_goal_ends = ends
    s._coverage_goal_frozen = frozen
    if any(status in {"exercised", "blocked"} for _, status, _ in changes):
        s._coverage_evidence_cursor = cursor
    return None
