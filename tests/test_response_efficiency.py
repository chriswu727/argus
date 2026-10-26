from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from argus.application.progress import progress_scope
from argus.domain.contracts import ErrorCode, OperationResult, ToolText
from argus.entrypoints.mcp.overhead import discovery_bytes, response_bytes
from argus.entrypoints.mcp.results import to_mcp_result
from argus.entrypoints.mcp.server import create_server
from argus.entrypoints.mcp.snapshots import FIELDS, ObservationSnapshots
from argus.measurement import measurement_scope, timed


def observation(**changes):
    return {
        "page": {"url": "https://example.test/", "title": "Fixture"},
        "targets": [{"description": f"button Item {i}", "disabled": False} for i in range(40)],
        "visible_text": "Fixture " * 100,
        "counts": {"items": 40},
        "feedback": [],
        "coverage": {"goals": []},
        "next_checks": [],
        **changes,
    }


def reconstruct(previous, encoded):
    snapshot = encoded["snapshot"]
    if snapshot["mode"] == "full":
        return {key: value for key, value in encoded.items() if key != "snapshot"}
    assert previous[0] == snapshot["base_id"]
    result = {
        key: value
        for key, value in previous[1].items()
        if key in FIELDS and key not in snapshot["removed"]
    }
    result.update({key: value for key, value in encoded.items() if key != "snapshot"})
    return result


def test_delta_roundtrip_including_removal_empty_values_and_non_snapshot_fields():
    cache = ObservationSnapshots()
    previous = None
    values = [
        observation(protocol="start only"),
        observation(
            counts={}, feedback=["Saved"], action={"executed": True, "application_passed": None}
        ),
        observation(targets=[], visible_text="", feedback=[]),
    ]
    del values[-1]["coverage"]
    for index, value in enumerate(values):
        encoded = cache.encode(value, "start_session" if index == 0 else "click_what", "run")
        decoded = reconstruct(previous, encoded)
        assert decoded == value
        previous = (encoded["snapshot"]["id"], decoded)
    assert encoded["snapshot"]["removed"] == ["coverage"]
    assert "protocol" not in decoded and "action" not in decoded


@pytest.mark.parametrize(
    "operation", ["observe", "navigate", "go_back", "start_session", "verify_persistence"]
)
def test_explicit_resync_and_navigation_always_return_full(operation):
    cache = ObservationSnapshots()
    cache.encode(observation(), "start_session", "run")
    assert cache.encode(observation(), operation, "run")["snapshot"]["mode"] == "full"


@pytest.mark.parametrize("reset", ["run", "url", "error", "unmigrated", "end"])
def test_snapshots_never_reuse_invalid_bases(reset):
    cache = ObservationSnapshots()
    cache.encode(observation(), "start_session", "run")
    data, run = observation(), "run"
    if reset == "run":
        run = "other-run"
    elif reset == "url":
        data["page"]["url"] += "next"
    else:
        cache.encode({}, "other", None if reset == "end" else run, error=reset == "error")
    assert cache.encode(data, "click_what", run)["snapshot"]["mode"] == "full"


def test_bounded_cache_periodic_full_no_mutation_and_small_payload_fallback():
    cache = ObservationSnapshots(max_deltas=1)
    original = observation()
    cache.encode(original, "start_session", "run")
    original["targets"].clear()
    assert cache.base["targets"]
    assert cache.encode(observation(), "click_what", "run")["snapshot"]["mode"] == "delta"
    assert cache.encode(observation(), "click_what", "run")["snapshot"]["mode"] == "full"
    tiny = {"page": {"url": "a"}}
    cache.encode(tiny, "start_session", "run")
    assert cache.encode(tiny, "click_what", "run")["snapshot"]["mode"] == "full"
    cache.max_bytes = 1
    assert cache.encode(original, "click_what", "run") is original
    assert cache.base == {} and cache.base_id is None


def test_redaction_before_retention_and_nested_verdict_preservation():
    cache = ObservationSnapshots()
    value = observation(visible_text="token=synthetic-secret-0123456789")
    with progress_scope() as progress:
        for match in (True, False, None):
            result = to_mcp_result(
                "test_action",
                OperationResult(
                    ToolText(
                        "legacy",
                        {
                            "observation": value,
                            "verification": {"matches": match},
                            "finding_confirmed": False,
                        },
                    ),
                    state="active",
                    run_id="run",
                ),
                progress,
                "delta",
                cache,
            )
            assert result.structuredContent["data"]["verification"]["matches"] is match
            assert result.structuredContent["data"]["finding_confirmed"] is False
        assert "synthetic-secret" not in json.dumps(cache.base)
        failed = to_mcp_result(
            "observe",
            OperationResult(ToolText("failure", value), error=ErrorCode.BACKEND, run_id="run"),
            progress,
            "delta",
            cache,
        )
    assert failed.isError and "failure" in failed.content[0].text
    assert cache.base == {}


def test_byte_accounting_handles_unicode_images_and_does_not_mutate():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="é"),
            ImageContent(type="image", data="AAAA", mimeType="image/png"),
        ],
        structuredContent={"value": "é"},
    )
    before = deepcopy(result)
    counts = response_bytes(result)
    assert counts["text_utf8_bytes"] == 2
    assert counts["image_base64_bytes"] == 4 and counts["images"] == 1
    assert counts["result_json_bytes"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True).encode()
    )
    assert result == before


async def test_measurements_are_request_local_and_record_failure_and_cancellation():
    @timed("browser_wait")
    async def operation(fail=False):
        await asyncio.sleep(0)
        if fail:
            raise RuntimeError("synthetic")

    async def run(fail):
        with measurement_scope() as measurements:
            try:
                await operation(fail)
            except RuntimeError:
                pass
        return measurements

    first, second = await asyncio.gather(run(False), run(True))
    assert first.calls == second.calls == {"browser_wait": 1}
    assert first.seconds["browser_wait"] > 0

    @timed("report_export")
    def cancelled():
        raise asyncio.CancelledError()

    with measurement_scope() as measurements, pytest.raises(asyncio.CancelledError):
        cancelled()
    assert measurements.calls == {"report_export": 1}


async def test_monitor_counts_only_and_discovery_accounting(isolated_runtime):
    isolated_runtime.settings = replace(isolated_runtime.settings, response_mode="delta")
    server = create_server(isolated_runtime, "agent")
    result = await server.call_tool("observe", {})
    assert result.isError
    totals = isolated_runtime.snapshot()["overhead"]["response_totals"]
    assert totals["responses"] == 1
    assert all(isinstance(value, int) for value in totals.values())
    discovery = discovery_bytes(await server.list_tools(), server.instructions)
    assert discovery["tools"] == 19 and discovery["output_schema_json_bytes"] > 0


async def test_fixed_encoding_benchmark_preserves_evidence_and_measures_tradeoffs():
    from argus.evaluation.overhead import benchmark

    result = await benchmark()
    assert result["aggregate"]["reduction_percent"] > 20
    cases = {case["case"]: case for case in result["cases"]}
    assert all(case["lossless_roundtrip"] for case in cases.values())
    assert cases["navigation"]["delta_vs_compact_reduction_percent"] < 0
    assert result["discovery"]["agent"]["tools_json_bytes"] < 28_812


def test_output_schema_retains_validation_without_repeated_display_titles():
    from argus.entrypoints.mcp.results import AgentResult

    schema = AgentResult.model_json_schema()
    assert '"title"' not in json.dumps(schema)
    assert schema["properties"]["status"]["enum"] == ["completed", "error"]
    assert schema["properties"]["schema_version"]["const"] == 1
    assert {"operation", "status", "session_state", "run_id", "error_code"} <= set(
        schema["required"]
    )


def test_schema_title_cleanup_does_not_remove_a_property_named_title():
    from argus.entrypoints.mcp.results import _omit_schema_titles

    schema = {"title": "Object", "properties": {"title": {"title": "Title", "type": "string"}}}
    _omit_schema_titles(schema)
    assert schema == {"properties": {"title": {"type": "string"}}}


def test_independent_snapshot_encoders_do_not_share_retained_state():
    first, second = ObservationSnapshots(), ObservationSnapshots()
    first.encode(observation(), "start_session", "run")
    assert second.encode(observation(), "click_what", "run")["snapshot"]["mode"] == "full"


@pytest.mark.parametrize("mode", ["compact", "delta"])
def test_sparse_envelope_roundtrips_and_preserves_nondefault_metadata(mode):
    from argus.entrypoints.mcp.results import AgentResult

    value = OperationResult(ToolText("Evidence", {"matches": False}), run_id="fixture")
    with progress_scope() as progress:
        compatible = to_mcp_result("verify_persistence", value, progress)
        compact = to_mcp_result("verify_persistence", value, progress, mode)
        assert AgentResult.model_validate(compact.structuredContent) == AgentResult.model_validate(
            compatible.structuredContent
        )
        assert (
            not {"recovery", "progress_omitted", "progress_delivery_failed"}
            & compact.structuredContent.keys()
        )
        progress.omitted = 3
        progress.delivery_failed = True
        explicit = to_mcp_result("verify_persistence", value, progress, mode)
        assert explicit.structuredContent["progress_omitted"] == 3
        assert explicit.structuredContent["progress_delivery_failed"] is True
        failure = to_mcp_result(
            "verify_persistence", OperationResult(value.value, ErrorCode.BACKEND), progress, mode
        )
        assert failure.isError and failure.structuredContent["recovery"]
        assert "[backend_error]" in failure.content[0].text


async def test_input_schema_cleanup_retains_constraints_and_literal_title_fields(isolated_runtime):
    from jsonschema import Draft202012Validator

    server = create_server(isolated_runtime, "agent")
    tool = server._tool_manager.get_tool("end_session")
    original = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
    advertised = next(
        item.inputSchema for item in await server.list_tools() if item.name == "end_session"
    )
    assert len(json.dumps(advertised)) < len(json.dumps(original))
    valid = {
        "coverage_updates": [
            {"goal": ["save", "reload"], "status": "exercised", "evidence": "Observed reload"}
        ]
    }
    for payload in (
        valid,
        {"coverage_updates": [{"goal": "save", "status": "passed", "evidence": "Observed"}]},
        {"coverage_updates": [{"goal": 42, "status": "blocked"}]},
    ):
        assert Draft202012Validator(original).is_valid(payload) == Draft202012Validator(
            advertised
        ).is_valid(payload)
    from argus.entrypoints.mcp.results import _omit_schema_titles

    schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "object",
                "default": {"title": "Preserve"},
                "properties": {"title": {"title": "Display", "type": "string"}},
            }
        },
    }
    _omit_schema_titles(schema)
    assert schema["properties"]["title"]["default"] == {"title": "Preserve"}
    assert schema["properties"]["title"]["properties"]["title"] == {"type": "string"}
