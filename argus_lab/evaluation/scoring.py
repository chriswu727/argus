from __future__ import annotations

from statistics import median

from .models import Dataset, Trial


def _tokens(trial: Trial) -> int | None:
    if trial.input_tokens is None or trial.output_tokens is None:
        return None
    return trial.input_tokens + trial.output_tokens


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compare(dataset: Dataset) -> dict:
    cases = {case.id: case for case in dataset.cases}
    arms = {}
    for arm in ("baseline", "argus"):
        trials = [trial for trial in dataset.trials if trial.arm == arm]
        tp = fp = duplicates = expected = covered = goals = failures = violations = 0
        recalls = []
        tokens = []
        for trial in trials:
            case = cases[trial.case_id]
            matched = [finding.matched_bug for finding in trial.findings if finding.matched_bug]
            tp += len(set(matched))
            fp += sum(finding.matched_bug is None for finding in trial.findings)
            duplicates += len(matched) - len(set(matched))
            expected += len(case.expected_bugs)
            covered += len(trial.covered_goals)
            goals += len(case.goals)
            failures += not trial.completed
            usage = _tokens(trial)
            tokens.append(usage)
            violations += bool(
                trial.tool_calls > trial.budget.tool_calls
                or trial.duration_seconds > trial.budget.wall_seconds
                or (
                    usage is not None
                    and trial.budget.tokens is not None
                    and usage > trial.budget.tokens
                )
            )
            if case.expected_bugs:
                recalls.append(len(set(matched)) / len(case.expected_bugs))
        arms[arm] = {
            "trials": len(trials),
            "failed_trials": failures,
            "budget_violations": violations,
            "true_positives": tp,
            "false_positives": fp,
            "duplicate_findings": duplicates,
            "false_negatives": expected - tp,
            "recall": _ratio(tp, expected),
            "precision": _ratio(tp, tp + fp + duplicates),
            "goal_coverage": _ratio(covered, goals),
            "recall_range": [min(recalls), max(recalls)] if recalls else None,
            "median_seconds": median(trial.duration_seconds for trial in trials),
            "median_tool_calls": median(trial.tool_calls for trial in trials),
            "median_tokens": median(tokens) if all(token is not None for token in tokens) else None,
            "token_usage_missing_trials": sum(token is None for token in tokens),
            "clean_case_false_positives": sum(
                len(t.findings) for t in trials if not cases[t.case_id].expected_bugs
            ),
        }
    deltas = {}
    for key in (
        "recall",
        "precision",
        "goal_coverage",
        "median_seconds",
        "median_tool_calls",
        "median_tokens",
    ):
        baseline, argus = arms["baseline"][key], arms["argus"][key]
        deltas[key] = argus - baseline if baseline is not None and argus is not None else None
    return {
        "schema_version": 1,
        "experiment": dataset.experiment,
        "mode": dataset.mode,
        "paired_trials": len(dataset.trials) // 2,
        "arms": arms,
        "argus_minus_baseline": deltas,
        "limitations": [
            "Scripted smoke results validate the harness, not agent effectiveness."
            if dataset.mode == "scripted_smoke"
            else "Results depend on supplied independent adjudication and the sampled tasks/models.",
            "Missing token usage is unknown, not zero. Failed and over-budget trials remain included.",
            "A reproduced symptom is not, by itself, proof that a reported behavior is a defect.",
        ],
    }
