from __future__ import annotations

import json

import pytest

from argus_lab.evaluation.adjudication import assemble


@pytest.fixture
def collection(tmp_path):
    reviews = {}
    for index, arm in enumerate(("baseline", "argus")):
        directory = tmp_path / f"run-{index:03d}"
        directory.mkdir()
        raw = {
            "arm": arm,
            "case_id": "profile-buggy",
            "repetition": 0,
            "model": "test-model",
            "host": "test-host",
            "fixture_revision": "fixture-v1",
            "task_revision": "task-v1",
            "seed": 0,
            "budget": {"tool_calls": 60, "wall_seconds": 300},
            "completed": True,
            "error": None,
            "duration_seconds": 1,
            "tool_calls": 2,
            "report": {"findings": [{"evidence_calls": [2, 3]}]},
        }
        (directory / "attempt.json").write_text("{}")
        (directory / "result.json").write_text(json.dumps(raw))
        (directory / "tools.jsonl").write_text(
            "\n".join(json.dumps({"call": n, "is_error": False}) for n in (1, 2))
        )
        reviews[directory.name] = {
            "findings": [
                {
                    "claim_index": 0,
                    "matched_bug": "profile-contract",
                    "evidence_calls": [1, 2],
                    "rationale": "Trace shows save then lost value. Corrected model's offset citations.",
                }
            ],
            "goals": [
                {
                    "goal": "reload-name",
                    "evidence_calls": [2],
                    "rationale": "Fresh load checked the saved name.",
                }
            ],
        }
    return tmp_path, reviews


def test_review_can_correct_citations_without_rewriting_model_answer(collection):
    directory, reviews = collection
    dataset = assemble(directory, reviews)
    assert dataset.trials[0].findings[0].evidence == ["run-000/call-1", "run-000/call-2"]
    raw = json.loads((directory / "run-000/result.json").read_text())
    assert raw["report"]["findings"][0]["evidence_calls"] == [2, 3]


def test_review_cannot_drop_a_claim_or_attempt(collection):
    directory, reviews = collection
    reviews["run-000"]["findings"] = []
    with pytest.raises(ValueError, match="every model claim"):
        assemble(directory, reviews)
    del reviews["run-000"]
    with pytest.raises(ValueError, match="every attempted result"):
        assemble(directory, reviews)


def test_review_cannot_confirm_missing_tool_evidence(collection):
    directory, reviews = collection
    reviews["run-000"]["findings"][0]["evidence_calls"] = [99]
    with pytest.raises(ValueError, match="successful tool evidence"):
        assemble(directory, reviews)


def test_unsupported_claim_and_failed_trial_remain_in_dataset(collection):
    directory, reviews = collection
    reviews["run-000"]["findings"][0].update(matched_bug=None, evidence_calls=[])
    path = directory / "run-000/result.json"
    raw = json.loads(path.read_text())
    raw.update(completed=False, error="timeout")
    path.write_text(json.dumps(raw))
    dataset = assemble(directory, reviews)
    assert not dataset.trials[0].completed
    assert dataset.trials[0].findings[0].matched_bug is None
    assert dataset.trials[0].findings[0].evidence == ["run-000/unsupported-claim"]


def test_historical_cases_require_explicit_reviewed_ground_truth(collection):
    from argus_lab.evaluation.models import Case

    directory, reviews = collection
    for review in reviews.values():
        review["findings"][0]["matched_bug"] = "corrected-bug"
    with pytest.raises(ValueError, match="outside the case ground truth"):
        assemble(directory, reviews)
    cases = [Case(id="profile-buggy", expected_bugs=["corrected-bug"], goals=["reload-name"])]
    assert assemble(directory, reviews, cases=cases).cases == cases
