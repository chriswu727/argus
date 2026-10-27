from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import Field, TypeAdapter

from argus.storage import atomic_write_text

from .agent_fixture import CASES
from .models import Case, Dataset, Finding, Record, Trial


class FindingDecision(Record):
    claim_index: int = Field(ge=0)
    matched_bug: str | None
    evidence_calls: list[int] = Field(max_length=20)
    rationale: str = Field(min_length=1, max_length=2000)


class GoalDecision(Record):
    goal: str
    evidence_calls: list[int] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=2000)


class Review(Record):
    findings: list[FindingDecision]
    goals: list[GoalDecision]


def assemble(directory: Path, reviews: dict, *, cases: list[Case] | None = None) -> Dataset:
    results = sorted(directory.glob("run-*/result.json"))
    if {path.parent.name for path in results} != set(reviews):
        raise ValueError("review every attempted result exactly once")
    if len(list(directory.glob("run-*/attempt.json"))) != len(results):
        raise ValueError("an interrupted attempt lacks a finalized result")
    trials = []
    for path in results:
        run = path.parent.name
        raw = json.loads(path.read_text())
        review = Review.model_validate(reviews[run])
        trace_path = path.parent / "tools.jsonl"
        calls = (
            {
                row["call"]: row
                for row in (json.loads(line) for line in trace_path.read_text().splitlines())
            }
            if trace_path.exists()
            else {}
        )
        successful = {number for number, call in calls.items() if not call["is_error"]}
        claims = raw.get("report", {}).get("findings", [])
        if sorted(decision.claim_index for decision in review.findings) != list(range(len(claims))):
            raise ValueError(
                "every model claim must be adjudicated; do not discard false positives"
            )
        findings = []
        for decision in review.findings:
            references = decision.evidence_calls
            if decision.matched_bug and (not references or not set(references) <= successful):
                raise ValueError("confirmed findings require valid successful tool evidence")
            findings.append(
                Finding(
                    id=f"{run}-claim-{decision.claim_index}",
                    matched_bug=decision.matched_bug,
                    evidence=[f"{run}/call-{number}" for number in references if number in calls]
                    or [f"{run}/unsupported-claim"],
                )
            )
        goals = {}
        for goal in review.goals:
            if goal.goal in goals or not set(goal.evidence_calls) <= successful:
                raise ValueError(
                    "goal reviews require distinct goals and successful trace references"
                )
            goals[goal.goal] = [f"{run}/call-{number}" for number in goal.evidence_calls]
        raw.pop("report", None)
        trials.append(
            Trial(
                **raw,
                findings=findings,
                covered_goals=goals,
                adjudicator="trace-and-server-oracle-review-v1",
            )
        )
    case_ids = {trial.case_id for trial in trials}
    return Dataset(
        experiment="agent-first-08-codex",
        mode="agent",
        cases=[case for case in (CASES if cases is None else cases) if case.id in case_ids],
        trials=trials,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Validate independent trace reviews and produce a paired dataset"
    )
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument(
        "--cases", type=Path, help="Explicit reviewed ground truth for historical runs"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = TypeAdapter(list[Case]).validate_json(args.cases.read_text()) if args.cases else None
    dataset = assemble(args.collection, json.loads(args.reviews.read_text()), cases=cases)
    atomic_write_text(args.output, dataset.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
