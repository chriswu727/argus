"""Deterministic encoding benchmark; no model, browser, or network calls."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from importlib.metadata import version

from argus import __version__
from argus.application.progress import progress_scope
from argus.bootstrap import create_runtime
from argus.domain.contracts import OperationResult, ToolText
from argus.entrypoints.mcp.overhead import discovery_bytes, response_bytes
from argus.entrypoints.mcp.results import to_mcp_result
from argus.entrypoints.mcp.server import create_server
from argus.entrypoints.mcp.snapshots import FIELDS, ObservationSnapshots


def fixture(case: str, step: int) -> dict:
    large = case != "small-page"
    page = step if case == "navigation" else 0
    return {
        "page": {"url": f"https://example.test/page-{page}", "title": "Fixture"},
        "visible_text": "Synthetic catalog items. " * (60 if large else 1),
        "targets": [
            {"description": f"button Item {i}", "disabled": False}
            for i in range(60 if large else 1)
        ],
        "counts": {"items": 60 if large else 1},
        "feedback": [f"Updated item {step}"] if case == "changed-page" else [],
        "coverage": {"goals": [{"goal": "exercise items", "status": "in_progress"}]},
        "count_changes": {},
        "next_checks": [{"kind": "goal", "reason": "Goal remains in progress"}],
        "action": {"kind": "click_what", "executed": True, "application_passed": None},
    }


async def benchmark() -> dict:
    runtime = create_runtime()
    discovery = {}
    for profile in ("agent", "core", "screen", "full"):
        server = create_server(runtime, profile)
        discovery[profile] = discovery_bytes(await server.list_tools(), server.instructions)
    cases = []
    for case in ("unchanged-page", "changed-page", "navigation", "small-page"):
        totals = {mode: Counter() for mode in ("compatible", "compact", "delta")}
        snapshots = ObservationSnapshots()
        previous = {}
        previous_id = None
        for step in range(6):
            data = fixture(case, step)
            result = OperationResult(
                ToolText(json.dumps(data), data), state="active", run_id="synthetic-run"
            )
            for mode in totals:
                with progress_scope() as progress:
                    encoded = to_mcp_result(
                        "start_session" if step == 0 else "click_what",
                        result,
                        progress,
                        mode,
                        snapshots,
                    )
                totals[mode].update(response_bytes(encoded))
                if mode == "delta":
                    observed = dict(encoded.structuredContent["data"])
                    snapshot = observed.pop("snapshot")
                    if snapshot["mode"] == "delta":
                        assert snapshot["base_id"] == previous_id
                        base = {
                            key: value
                            for key, value in previous.items()
                            if key in FIELDS and key not in snapshot["removed"]
                        }
                        observed = {**base, **observed}
                    assert observed == data, "Encoding lost observation evidence"
                    previous, previous_id = observed, snapshot["id"]
        compact_bytes = totals["compact"]["result_json_bytes"]
        delta_bytes = totals["delta"]["result_json_bytes"]
        cases.append(
            {
                "case": case,
                "calls": 6,
                "lossless_roundtrip": True,
                "modes": totals,
                "delta_vs_compact_reduction_percent": round(
                    100 * (1 - delta_bytes / compact_bytes), 2
                ),
            }
        )
    compact = sum(case["modes"]["compact"]["result_json_bytes"] for case in cases)
    delta = sum(case["modes"]["delta"]["result_json_bytes"] for case in cases)
    return {
        "schema_version": 1,
        "argus_version": __version__,
        "mcp_sdk_version": version("mcp"),
        "method": "synthetic-identical-input-encoding-v1",
        "baseline": "0.8-compatible compact encoder semantics; same structured evidence",
        "limitations": [
            "No model calls or token measurement",
            "Not an end-to-end latency or testing-quality benchmark",
            "Result JSON excludes JSON-RPC framing; byte categories overlap",
            "Synthetic repeated catalog content favors deltas; navigation invalidates them",
        ],
        "discovery": discovery,
        "cases": cases,
        "aggregate": {
            "compact_result_json_bytes": compact,
            "delta_result_json_bytes": delta,
            "reduction_percent": round(100 * (1 - delta / compact), 2),
        },
    }


def main():
    print(json.dumps(asyncio.run(benchmark()), indent=2))


if __name__ == "__main__":
    main()
