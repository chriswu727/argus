from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import argus.mcp_server as m
from argus.adapters.reporting.common import _repro_badge
from argus.application.findings import record_bug
from argus.application.probes import verify_persistence
from argus.application.progress import progress_scope
from argus.differ import compute_changes
from argus.domain.contracts import OperationResult, Rejection, ToolText
from argus.entrypoints.mcp.results import to_mcp_result
from argus_lab.evaluation.agent_fixture import agent_fixture


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


@pytest.mark.parametrize(
    "expect,target,page,status,expect_status,matches",
    [
        ("absent", "Buy milk", "Not found", 404, 0, None),
        ("absent", "Buy milk", "Not found", 404, 404, True),
        ("absent", "Buy milk", "Buy milk", 200, 404, False),
        ("absent", "Buy milk", "Please log in", 200, 0, None),
        ("present", "Please log in", "Please log in", 200, 0, True),
        ("absent", "Buy milk", "Buy milk", 500, 0, False),
    ],
)
async def test_only_absence_on_error_or_login_pages_is_inconclusive(
    isolated_runtime, page_factory, expect, target, page, status, expect_status, matches
):
    isolated_runtime.session.mode = "web"
    isolated_runtime.session.browser = SimpleNamespace(
        current_url="https://example.test/items/5",
        has_page=True,
        get_state=AsyncMock(return_value=page_factory(page_text=page)),
        goto=AsyncMock(return_value=SimpleNamespace(status=status)),
        scroll_by=AsyncMock(),
    )
    result = await verify_persistence(expect, target, expect_status=expect_status)
    check = result.data["verification"]
    assert check["matches"] is matches and check["status"] == status
    assert check["conclusive"] is (matches is not None)
    assert ("INCONCLUSIVE" in result) is (matches is None)


async def test_persistence_rejects_invalid_expected_status(isolated_runtime):
    isolated_runtime.session.mode = "web"
    isolated_runtime.session.browser = SimpleNamespace(current_url="https://example.test/")
    assert isinstance(await verify_persistence("absent", "x", expect_status=42), Rejection)


async def test_before_state_scrolls_for_a_just_saved_row(isolated_runtime, page_factory):
    states = [page_factory(page_text="Top"), page_factory(page_text="Morgan"), page_factory()]
    isolated_runtime.session.mode = "web"
    isolated_runtime.session.browser = SimpleNamespace(
        current_url="https://example.test/",
        has_page=True,
        get_state=AsyncMock(side_effect=states + [page_factory()] * 25),
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        scroll_by=AsyncMock(),
    )
    result = await verify_persistence("present", "Morgan")
    assert result.data["verification"]["before_matches"] is True
    assert result.data["suggested_verify"]["expect"] == "absent"


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
        {
            "previous_receipt": None,
            "verify": {"expect": "absent"},
            "receipt": {**verifier.return_value, "recheck": 1, "previous_verdict": "observed"},
        }
    ]
    assert "recheck 1, earlier observed" in _repro_badge(bug.reproduction_receipt)
    assert bug.to_dict()["verification_history"] == bug.verification_history
    again = await record_bug(
        "Saved name lost", "high", verify={"expect": "absent"}, finding_id=finding_id
    )
    assert again.data["reason"] == "already_verified"
    assert verifier.await_count == 1


async def test_interrupted_recheck_consumes_its_slot_with_an_explicit_receipt(isolated_runtime):
    s = isolated_runtime.session
    s.screen = object()
    first = await record_bug("Failure", "low", {"screenshot": "skip"})
    isolated_runtime.verifiers.verify = AsyncMock(side_effect=TimeoutError("synthetic"))
    with pytest.raises(TimeoutError):
        await record_bug(
            "Failure", "low", verify={"replay": True}, finding_id=first.data["finding_id"]
        )
    (entry,) = s.bugs[0].verification_history
    assert entry["receipt"]["reproduced"] is None
    assert "did not complete" in entry["receipt"]["reason"]
    assert s.bugs[0].reproduction_receipt is None


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
    assert "Field values changed: 1" in str(changes)
    assert "Visible page text changed" not in str(changes)
    assert "SECRET" not in str(changes) and "No visible changes" not in str(changes)
    assert compute_changes(before, before) == ["No visible changes detected"]
    text_only = page_factory(
        page_text="Display name: Morgan",
        elements=[element_factory(tag="input", value="SECRET_OLD")],
    )
    assert compute_changes(before, text_only) == [
        "Visible page text changed; inspect the post-action observation"
    ]


def test_inserted_rows_do_not_read_as_field_value_changes(page_factory, element_factory):
    def row(index, label, value):
        return element_factory(index=index, tag="input", placeholder=label, value=value)

    before = page_factory(elements=[row(0, "Task A", "a"), row(1, "Task B", "b")])
    after = page_factory(
        elements=[row(0, "Task New", "n"), row(1, "Task A", "a"), row(2, "Task B", "b")]
    )
    assert "Field values changed" not in str(compute_changes(before, after))
    edited = page_factory(elements=[row(0, "Task A", "a"), row(1, "Task B", "edited")])
    assert "Field values changed: 1" in str(compute_changes(before, edited))


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
