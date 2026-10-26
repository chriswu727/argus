from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import argus.mcp_server as m
from argus.application.findings import record_bug
from argus.application.probes import verify_persistence
from argus.application.progress import progress_scope
from argus.differ import compute_changes
from argus.domain.contracts import OperationResult, Rejection, ToolText
from argus.entrypoints.mcp.results import to_mcp_result
from argus.evaluation.agent_fixture import agent_fixture


@pytest.mark.parametrize("variant", ["buggy", "fixed"])
async def test_persistence_workflow_never_repeats_write(variant, isolated_runtime):
    with agent_fixture(f"profile-{variant}") as (origin, events):
        await m.start_session(origin)
        try:
            await m.test_form({"Display name": "Morgan"}, "Save")
            result = await m.verify_persistence("present", "Morgan")
            assert result.data["verification"]["before_matches"] is True
            assert result.data["verification"]["matches"] is (variant == "fixed")
            if variant == "buggy":
                verify = result.data["suggested_verify"]
                assert verify["expect"] == "absent" and not verify.get("replay")
                recorded = await m.record_bug(
                    "Saved name is lost", "high", {"screenshot": "skip"}, verify
                )
                assert recorded.data["verdict"] == "verified"
                assert recorded.data["receipt"]["runs"] == "2/2"
            else:
                assert result.data["suggested_verify"] is None
            assert sum(event["method"] == "POST" for event in events) == 1
        finally:
            await m.end_session()


@pytest.mark.parametrize(
    "before,after,status,suggested",
    [
        ("Morgan", "Alex", 200, True),
        ("Alex", "Alex", 200, False),
        ("Morgan", "Please log in", 200, False),
        ("Morgan", "Not found", 404, False),
        ("Morgan", "Morgan", 200, False),
    ],
)
async def test_persistence_suggestions_require_observed_transition(
    isolated_runtime, page_factory, before, after, status, suggested
):
    browser = SimpleNamespace(
        current_url="https://example.test/",
        has_page=True,
        get_state=AsyncMock(
            side_effect=[page_factory(page_text=before), page_factory(page_text=after)]
        ),
        goto=AsyncMock(return_value=SimpleNamespace(status=status)),
    )
    isolated_runtime.session.browser = browser
    isolated_runtime.session.mode = "web"
    from unittest.mock import patch

    with patch(
        "argus.application.probes._present_with_scroll", AsyncMock(return_value=after == "Morgan")
    ):
        result = await verify_persistence("present", "Morgan")
    assert bool(result.data["suggested_verify"]) is suggested
    assert result.data["finding_confirmed"] is False
    if status >= 400 or after == "Please log in":
        assert result.data["verification"]["matches"] is None
        assert "INCONCLUSIVE" in result


async def test_recheck_uses_id_preserves_history_and_never_adds_duplicate(isolated_runtime):
    session = isolated_runtime.session
    session.screen = object()
    first = await record_bug("Saved name lost", "high", {"screenshot": "skip"})
    finding_id = first.data["finding_id"]
    duplicate = await record_bug("Saved name lost", "high", {"screenshot": "skip"})
    assert duplicate.data["duplicate_of"] == finding_id
    verifier = AsyncMock(return_value={"attempted": True, "reproduced": True, "runs": "2/2"})
    isolated_runtime.verifiers.verify = verifier
    result = await record_bug(
        "Saved name lost", "high", verify={"expect": "absent"}, finding_id=finding_id
    )
    assert result.data["updated"] is True and result.data["verdict"] == "verified"
    assert len(session.bugs) == 1
    bug = session.bugs[0]
    assert bug.verification_history == [
        {"previous_receipt": None, "receipt": verifier.return_value}
    ]
    assert bug.to_dict()["verification_history"] == bug.verification_history
    again = await record_bug(
        "Saved name lost", "high", verify={"expect": "absent"}, finding_id=finding_id
    )
    assert again.data["reason"] == "already_verified"
    assert verifier.await_count == 1


async def test_rechecks_are_bounded_and_keep_original_trace(isolated_runtime):
    s = isolated_runtime.session
    s.screen = object()
    s.action_trace.append({"tool": "navigate", "value": "https://example.test/"})
    first = await record_bug("Failure", "low", {"screenshot": "skip"})
    s.action_trace.append({"tool": "click_what", "description": "New unrelated action"})
    cursor = s._actions_since_last_bug
    verify = AsyncMock(return_value={"attempted": True, "reproduced": None})
    isolated_runtime.verifiers.verify = verify
    for _ in range(2):
        result = await record_bug(
            "Failure", "low", verify={"replay": True}, finding_id=first.data["finding_id"]
        )
        assert result.data["verdict"] == "inconclusive"
    result = await record_bug(
        "Failure", "low", verify={"replay": True}, finding_id=first.data["finding_id"]
    )
    assert result.data["reason"] == "recheck_limit"
    assert verify.await_count == 2
    assert verify.call_args.args[2] == s.bugs[0].replay_steps
    assert len(verify.call_args.args[2]) == 1
    assert s._actions_since_last_bug == cursor


async def test_invalid_recheck_never_touches_browser_or_finding(isolated_runtime):
    s = isolated_runtime.session
    s.screen = object()
    first = await record_bug("Failure", "low", {"screenshot": "skip"})
    verify = AsyncMock()
    isolated_runtime.verifiers.verify = verify
    for args in (
        {"finding_id": "other-run:1", "verify": {"expect": "present"}},
        {"finding_id": first.data["finding_id"]},
        {"finding_id": first.data["finding_id"], "verify": {"expect": "present"}, "evidence": {}},
    ):
        assert isinstance(await record_bug("Failure", "low", **args), Rejection)
    assert not verify.called and not s.bugs[0].verification_history


async def test_recheck_preserves_expired_capsule_guard(isolated_runtime, page_factory):
    s = isolated_runtime.session
    s.screen = object()
    first = await record_bug("Failure", "low", {"screenshot": "skip"})
    s.mode = "web"
    s._capsule_marker = "Signed in as Morgan"
    s.browser = SimpleNamespace(
        has_page=True, get_state=AsyncMock(return_value=page_factory(page_text="Please log in"))
    )
    isolated_runtime.verifiers.verify = AsyncMock(
        return_value={"attempted": True, "reproduced": True}
    )
    result = await record_bug(
        "Failure", "low", verify={"expect": "absent"}, finding_id=first.data["finding_id"]
    )
    assert result.data["verdict"] == "inconclusive"
    assert "expired" in result.data["receipt"]["reason"]


def test_diff_reports_text_and_value_changes_without_exposing_values(page_factory, element_factory):
    before = page_factory(
        page_text="Display name: Alex", elements=[element_factory(tag="input", value="SECRET_OLD")]
    )
    after = page_factory(
        page_text="Display name: Morgan",
        elements=[element_factory(tag="input", value="SECRET_NEW")],
    )
    changes = compute_changes(before, after, "form submission")
    assert "Visible page text changed" in str(changes)
    assert "Field values changed: 1" in str(changes)
    assert "SECRET" not in str(changes) and "No visible changes" not in str(changes)
    assert compute_changes(before, before) == ["No visible changes detected"]


@pytest.mark.parametrize("mode", ["compact", "delta"])
@pytest.mark.parametrize("nested", [True, False])
def test_shared_coverage_evidence_roundtrips_without_mutation(mode, nested):
    refs = {
        "actions": [{"tool": "click_what", "description": "Save"}] * 20,
        "warnings": ["incomplete"],
        "api_key": "SYNTHETIC_SECRET",
    }
    coverage = {"goals": [{"goal": name, "evidence_refs": refs} for name in ("save", "reload")]}
    payload = {"observation": {"coverage": coverage}} if nested else {"coverage": coverage}
    original = deepcopy(payload)
    with progress_scope() as progress:
        compatible = to_mcp_result(
            "end_session", OperationResult(ToolText("done", payload)), progress
        )
        compact = to_mcp_result(
            "end_session", OperationResult(ToolText("done", payload)), progress, mode
        )
    data = compact.structuredContent["data"]
    transformed = (data["observation"] if nested else data)["coverage"]
    assert len(transformed["evidence"]) == 1
    for goal in transformed["goals"]:
        goal["evidence_refs"] = transformed["evidence"][goal.pop("evidence_ref")]
    transformed.pop("evidence")
    assert data == compatible.structuredContent["data"]
    assert payload == original
    assert "SYNTHETIC_SECRET" not in compact.model_dump_json()
    with progress_scope() as progress:
        encoded = to_mcp_result(
            "end_session", OperationResult(ToolText("done", payload)), progress, mode
        )
    assert (
        len(json.dumps(encoded.structuredContent))
        < len(json.dumps(compatible.structuredContent)) * 0.75
    )
