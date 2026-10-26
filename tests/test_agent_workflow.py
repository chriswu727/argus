from __future__ import annotations

import json
from dataclasses import replace

import pytest

from argus.application.expectations import _evaluate_expectation
from argus.application.guidance import monitor_counts
from argus.application.payloads import observation_data
from argus.application.progress import progress_scope
from argus.domain.contracts import OperationResult, ToolText
from argus.entrypoints.mcp.results import to_mcp_result
from argus.settings import Settings


@pytest.mark.parametrize("before,after", [({}, {"items": 1}), ({"items": 1}, {}), ({}, {})])
def test_missing_count_is_unknown_not_zero(page_factory, before, after):
    result = _evaluate_expectation(
        page_factory(counts=before),
        page_factory(counts=after),
        {"count": {"label": "items", "delta": 1}},
    )
    assert result[0][1] is None


@pytest.mark.parametrize(
    "expect",
    [
        {"text_present": ""},
        {"text_absent": None},
        {"gains": 42},
        {"gains": [False]},
        {"removes": []},
        {"url_changed": "perhaps"},
        {"toast": ["Saved"]},
        {"count": {"label": "items", "delta": 1.5}},
        {"count": {"label": "items", "delta": True}},
        {"count": {"label": "items", "value": 1, "delta": 1}},
    ],
)
def test_malformed_expectations_never_become_match_or_surprise(page_factory, expect):
    result = _evaluate_expectation(page_factory(), page_factory(), expect)
    assert result and all(matches is None for _, matches, _ in result)


def test_count_delta_requires_same_page(page_factory):
    before = page_factory(url="https://example.test/a", counts={"items": 1})
    after = page_factory(url="https://example.test/b", counts={"items": 2})
    assert (
        _evaluate_expectation(before, after, {"count": {"label": "items", "delta": 1}})[0][1]
        is None
    )


def test_guidance_is_bounded_does_not_mark_goals_complete(
    isolated_runtime, page_factory, element_factory
):
    session = isolated_runtime.session
    session.coverage_goals = [{"goal": "edit profile", "status": "in_progress"}]
    session.action_trace.append({"tool": "click_what"})
    state = page_factory(elements=[element_factory(tag="input")])
    for _ in range(3):
        result = observation_data(session, state)
    kinds = [suggestion["kind"] for suggestion in result["next_checks"]]
    assert kinds == ["stalled", "goal", "persistence", "input_boundary"]
    assert session.coverage_goals[0]["status"] == "in_progress"
    session.exploration.last_verified_action = len(session.action_trace)
    assert "persistence" not in [s["kind"] for s in observation_data(session, state)["next_checks"]]


def test_compact_mode_preserves_data_and_compatibility_text():
    value = ToolText("verbose " * 1000, {"visible_text": "Saved", "warnings": ["incomplete"]})
    with progress_scope() as progress:
        compact = to_mcp_result("observe", OperationResult(value), progress, "compact")
        compatible = to_mcp_result("observe", OperationResult(value), progress)
    assert compatible.content[0].text == value
    assert compact.structuredContent == compatible.structuredContent
    assert len(compact.model_dump_json()) < len(compatible.model_dump_json()) / 2


def test_compact_mode_does_not_discard_unmigrated_tools_or_errors():
    from argus.domain.contracts import ErrorCode

    with progress_scope() as progress:
        result = to_mcp_result(
            "unmigrated", OperationResult("important evidence"), progress, "compact"
        )
    assert result.content[0].text == "important evidence"
    with progress_scope() as progress:
        failed = to_mcp_result(
            "observe",
            OperationResult(
                ToolText("failed to inspect", {"partial": True}), error=ErrorCode.BACKEND
            ),
            progress,
            "compact",
        )
    assert failed.isError
    assert "failed to inspect" in failed.content[0].text
    assert "completed" not in failed.content[0].text


def test_response_mode_is_operator_controlled():
    assert Settings.from_env({"ARGUS_RESPONSE_MODE": "compact"}).response_mode == "compact"
    with pytest.raises(ValueError, match="response_mode"):
        replace(Settings(), response_mode="unknown")


def test_monitor_and_live_progress_expose_counts_not_goal_text(isolated_runtime):
    session = isolated_runtime.session
    session.coverage_goals = [{"goal": "SYNTHETIC_PRIVATE_GOAL", "status": "in_progress"}]
    session.verification_checks = [{"target_text": "SYNTHETIC_PRIVATE_VALUE"}]
    counts = monitor_counts(session)
    assert counts["active_goal_indices"] == [1]
    assert counts["checks"] == 1
    assert "SYNTHETIC_PRIVATE" not in json.dumps(isolated_runtime.snapshot())


def test_monitor_distinguishes_unchecked_and_inconclusive_findings(isolated_runtime):
    from types import SimpleNamespace

    isolated_runtime.session.bugs = [
        SimpleNamespace(reproduction_receipt=None),
        SimpleNamespace(reproduction_receipt={"attempted": True, "reproduced": None}),
        SimpleNamespace(reproduction_receipt={"reproduced": True}),
        SimpleNamespace(reproduction_receipt={"reproduced": False}),
    ]
    assert monitor_counts(isolated_runtime.session)["verdicts"] == {
        "observed": 1,
        "inconclusive": 1,
        "reproduced": 1,
        "not_reproduced": 1,
    }


async def test_agent_profile_keeps_workflow_with_bounded_discovery(isolated_runtime):
    from argus.entrypoints.mcp.server import create_server

    agent = await create_server(isolated_runtime, "agent").list_tools()
    core = await create_server(isolated_runtime, "core").list_tools()
    assert len(agent) == 19
    assert {tool.name for tool in agent} <= {tool.name for tool in core}
    assert all(tool.outputSchema for tool in agent)

    def size(tools):
        return sum(len(tool.model_dump_json()) for tool in tools)

    assert size(agent) < size(core) * 0.65


async def test_batched_coverage_is_atomic_and_preserves_shared_evidence(isolated_runtime):
    from argus.application.observation import coverage_update
    from argus.domain.contracts import Rejection

    session = isolated_runtime.session
    session.screen = object()
    session.coverage_goals = [
        {"goal": "edit name", "status": "untested"},
        {"goal": "reload name", "status": "untested"},
    ]
    for queries in (["edit name", "missing"], ["edit name", "edit"], [], ["edit name"] * 21):
        assert isinstance(await coverage_update(queries, "in_progress"), Rejection)
        assert all(goal["status"] == "untested" for goal in session.coverage_goals)
    await coverage_update(["edit name", "reload name"], "in_progress")
    session.coverage_action_trace.append({"tool": "click_what", "description": "Save"})
    session.verification_checks.append({"matches": False})
    await coverage_update(["edit name", "reload name"], "exercised", "Saved then reloaded.")
    first, second = session.coverage_goals
    assert first["evidence_refs"] == second["evidence_refs"]
    assert len(first["evidence_refs"]["actions"]) == 1
    assert first["evidence_refs"]["verifications"] == [{"matches": False}]
