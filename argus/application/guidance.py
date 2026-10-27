from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from argus.domain.models import PageState


@dataclass
class ExplorationProgress:
    fingerprint: str = ""
    repeated_observations: int = 0
    last_verified_action: int = 0

    def observe(self, state: PageState, checks: int, findings: int) -> None:
        facts = [state.url, state.page_text, state.counts, checks, findings]
        fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
        self.repeated_observations = (
            self.repeated_observations + 1 if fingerprint == self.fingerprint else 0
        )
        self.fingerprint = fingerprint


def next_checks(session, state: PageState, coverage: dict) -> list[dict]:
    progress = session.exploration
    progress.observe(state, len(session.verification_checks), len(session.bugs))
    suggestions = []

    def add(kind, reason, action):
        suggestions.append({"kind": kind, "reason": reason, "action": action})

    if coverage["time_budget"]["exceeded"]:
        add(
            "finish",
            "The advisory time budget has elapsed.",
            "Finish safely and report uncovered goals.",
        )
        return suggestions
    if progress.repeated_observations >= 2:
        add(
            "stalled",
            "Repeated observations produced no new visible state or check evidence.",
            "Change the probe or target an untested goal; do not repeat a write blindly.",
        )
    pending = [g for g in coverage["goals"] if g["status"] in {"untested", "in_progress"}]
    if pending:
        add("goal", "A declared journey still lacks completion evidence.", pending[0]["goal"])
    writes = session.action_trace[progress.last_verified_action :]
    if any(a.get("tool") == "click_what" for a in writes):
        add(
            "persistence",
            "An interaction occurred after the last persistence check.",
            "If the interaction should save or delete data, verify the distinctive value after reload.",
        )
    if any(el.tag in {"input", "textarea", "select"} for el in state.elements):
        add(
            "input_boundary",
            "The page exposes editable fields.",
            "Within the approved task, compare a valid input with an empty or boundary input and check recovery.",
        )
    if coverage["pages"]["unvisited"]:
        add(
            "cross_page",
            "Discovered pages remain unvisited.",
            "Follow a relevant in-scope page and compare the same state across views.",
        )
    if not suggestions:
        add(
            "review",
            "No outstanding declared goal or specific probe is visible.",
            "Review evidence and scope; add a missing user journey or finish without claiming exhaustive coverage.",
        )
    return suggestions[:4]


def monitor_counts(session) -> dict:
    counts = {status: 0 for status in ("untested", "in_progress", "exercised", "blocked")}
    for goal in session.coverage_goals:
        if goal.get("status") in counts:
            counts[goal["status"]] += 1
    verdicts = {"reproduced": 0, "not_reproduced": 0, "inconclusive": 0, "observed": 0}
    for bug in session.bugs:
        if not bug.reproduction_receipt:
            verdicts["observed"] += 1
            continue
        reproduced = (bug.reproduction_receipt or {}).get("reproduced")
        key = (
            "reproduced"
            if reproduced is True
            else "not_reproduced"
            if reproduced is False
            else "inconclusive"
        )
        verdicts[key] += 1
    return {
        "goals": counts,
        "checks": len(session.verification_checks),
        "verdicts": verdicts,
        "active_goal_indices": [
            i + 1
            for i, goal in enumerate(session.coverage_goals)
            if goal.get("status") == "in_progress"
        ],
    }
