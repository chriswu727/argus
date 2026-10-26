from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.evaluation.models import Dataset
from argus.evaluation.scoring import compare


@pytest.mark.parametrize("cohort,count", [("pilot", 8), ("optimized", 20)])
def test_published_agent_datasets_are_complete_and_rescorable(cohort, count):
    source = Path(__file__).parents[1] / "examples" / f"agent-evaluation-{cohort}.json"
    dataset = Dataset.model_validate_json(source.read_text())
    assert len(dataset.trials) == count
    assert compare(dataset)["paired_trials"] == count // 2
    evidence = json.loads(source.with_name(f"agent-evaluation-{cohort}-evidence.json").read_text())
    assert len(evidence["runs"]) == count
    for trial in dataset.trials:
        for finding in trial.findings:
            for reference in finding.evidence:
                run, call = reference.split("/call-")
                assert int(call) in {
                    entry["call"] for entry in evidence["runs"][run]["tool_excerpts"]
                }


def dataset_payload():
    trial = {
        "case_id": "buggy",
        "repetition": 0,
        "model": "fixed-model-version",
        "host": "fixed-host-version",
        "fixture_revision": "sha256-fixture",
        "task_revision": "sha256-prompt",
        "seed": 7,
        "budget": {"tool_calls": 10, "wall_seconds": 60, "tokens": 2000},
        "completed": True,
        "duration_seconds": 3,
        "tool_calls": 4,
        "findings": [{"id": "f1", "matched_bug": "bug-1", "evidence": ["receipt-1"]}],
        "covered_goals": {"goal-1": ["trace-1"]},
        "adjudicator": "independent-oracle-v1",
    }
    return {
        "experiment": "paired-check",
        "mode": "agent",
        "cases": [
            {"id": "buggy", "expected_bugs": ["bug-1", "bug-2"], "goals": ["goal-1", "goal-2"]}
        ],
        "trials": [{**trial, "arm": "baseline"}, {**trial, "arm": "argus"}],
    }


def test_paired_metrics_missing_usage_and_duplicate_claims():
    payload = dataset_payload()
    payload["trials"][1]["findings"] = [
        {"id": "f1", "matched_bug": "bug-1", "evidence": ["receipt-1"]},
        {"id": "f2", "matched_bug": "bug-1", "evidence": ["receipt-2"]},
        {"id": "f3", "matched_bug": None, "evidence": ["receipt-3"]},
    ]
    result = compare(Dataset.model_validate(payload))
    arm = result["arms"]["argus"]
    assert arm["recall"] == 0.5
    assert arm["precision"] == 1 / 3
    assert arm["false_positives"] == arm["duplicate_findings"] == arm["false_negatives"] == 1
    assert arm["goal_coverage"] == 0.5
    assert arm["median_tokens"] is None
    assert result["argus_minus_baseline"]["median_tokens"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "other"),
        ("host", "other"),
        ("seed", 9),
        ("fixture_revision", "other"),
        ("task_revision", "other"),
        ("adjudicator", "other"),
        ("model_settings", {"temperature": 1}),
        ("budget", {"tool_calls": 20, "wall_seconds": 60}),
    ],
)
def test_mismatched_experimental_controls_are_rejected(field, value):
    payload = dataset_payload()
    payload["trials"][1][field] = value
    with pytest.raises(ValidationError, match="experimental controls"):
        Dataset.model_validate(payload)


def test_failures_and_budget_overruns_cannot_disappear_from_score():
    payload = dataset_payload()
    payload["trials"][1].update(
        completed=False, error="timeout", findings=[], covered_goals={}, tool_calls=11
    )
    arm = compare(Dataset.model_validate(payload))["arms"]["argus"]
    assert arm["failed_trials"] == arm["budget_violations"] == 1
    assert arm["recall"] == arm["goal_coverage"] == 0
    assert arm["precision"] is None


def test_known_usage_is_scored_and_token_budget_is_checked():
    payload = dataset_payload()
    for trial in payload["trials"]:
        trial.update(input_tokens=1800, output_tokens=300)
    result = compare(Dataset.model_validate(payload))
    assert result["arms"]["argus"]["median_tokens"] == 2100
    assert result["arms"]["argus"]["budget_violations"] == 1
    assert result["argus_minus_baseline"]["median_tokens"] == 0


def test_compare_cli_round_trip(tmp_path, monkeypatch, capsys):
    from argus.evaluation.__main__ import main

    source = tmp_path / "trials.json"
    output = tmp_path / "validated.json"
    source.write_text(json.dumps(dataset_payload()))
    monkeypatch.setattr(
        "sys.argv", ["evaluation", "--compare", str(source), "--output", str(output)]
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["paired_trials"] == 1
    assert Dataset.model_validate_json(output.read_text()).experiment == "paired-check"


def test_compare_cli_rejects_invalid_pairs_without_echoing_data(tmp_path, monkeypatch, capsys):
    from argus.evaluation.__main__ import main

    payload = dataset_payload()
    payload["trials"].pop()
    payload["experiment"] = "SYNTHETIC_PRIVATE_EXPERIMENT"
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(payload))
    monkeypatch.setattr("sys.argv", ["evaluation", "--compare", str(source)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "Invalid evaluation" in captured.err
    assert "SYNTHETIC_PRIVATE" not in captured.err + captured.out


@pytest.mark.parametrize(
    "mutation",
    [
        "unpaired",
        "duplicate",
        "unknown_case",
        "unknown_bug",
        "empty_evidence",
        "unknown_goal",
        "missing_error",
        "nan",
    ],
)
def test_malformed_trials_are_rejected(mutation):
    payload = dataset_payload()
    trial = payload["trials"][1]
    if mutation == "unpaired":
        payload["trials"].pop()
    elif mutation == "duplicate":
        payload["trials"].append(dict(trial))
    elif mutation == "unknown_case":
        trial["case_id"] = "missing"
    elif mutation == "unknown_bug":
        trial["findings"] = [{"id": "f1", "matched_bug": "missing", "evidence": ["receipt"]}]
    elif mutation == "empty_evidence":
        trial["covered_goals"] = {"goal-1": []}
    elif mutation == "unknown_goal":
        trial["covered_goals"] = {"unknown": ["trace"]}
    elif mutation == "missing_error":
        trial["completed"] = False
    else:
        trial["duration_seconds"] = float("nan")
    with pytest.raises(ValidationError):
        Dataset.model_validate(payload)


async def test_scripted_smoke_exercises_buggy_fixed_and_clean_controls():
    from argus.evaluation.smoke import run_smoke

    dataset = await run_smoke()
    result = compare(dataset)
    assert result["mode"] == "scripted_smoke"
    assert result["paired_trials"] == 3
    assert all(trial.tool_calls == 5 for trial in dataset.trials if trial.arm == "argus")
    for arm in result["arms"].values():
        assert arm["failed_trials"] == 0, dataset.model_dump_json(indent=2)
        assert arm["true_positives"] == 1
        assert arm["false_positives"] == arm["clean_case_false_positives"] == 0
    assert "not agent effectiveness" in json.dumps(result)
