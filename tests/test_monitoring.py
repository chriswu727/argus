from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from argus.adapters.monitoring import Monitor


@pytest.fixture
async def monitor(isolated_runtime):
    server = Monitor(isolated_runtime, 0)
    await server.start()
    try:
        yield server
    finally:
        isolated_runtime.events.close()
        await server.stop()


async def test_monitor_health_status_assets_and_host_guards(monitor):
    async with httpx.AsyncClient(base_url=monitor.url) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).json() == {"ready": True}
        assert (await client.get("/api/status")).json()["state"] == "idle"
        assert "Live session" in (await client.get("/")).text
        assert (await client.get("/dashboard.js")).status_code == 200
        assert (await client.get("/healthz", headers={"Host": "evil.example"})).status_code == 403
        assert (
            await client.get("/healthz", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (await client.get("/healthz", headers={"Origin": "http://["})).status_code == 403
        assert (
            await client.get("/healthz", headers={"Sec-Fetch-Site": "cross-site"})
        ).status_code == 403
        assert (
            await client.get("/api/events", headers={"Last-Event-ID": "nope"})
        ).status_code == 400
        monitor.runtime.ready = False
        assert (await client.get("/readyz")).status_code == 503


async def test_monitor_events_and_metrics_have_no_inputs(monitor):
    async def operation(text):
        return text

    await monitor.runtime.execute("type_into", operation, text="synthetic-sensitive-input")
    async with httpx.AsyncClient(base_url=monitor.url) as client:
        status = await client.get("/api/status")
        assert "synthetic-sensitive-input" not in status.text
        metrics = await client.get("/metrics")
        assert 'argus_operation_calls_total{operation="type_into"} 1' in metrics.text
        async with client.stream("GET", "/api/events") as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    assert event["operation"] == "type_into"
                    assert "synthetic-sensitive-input" not in line
                    break


async def test_mcp_overhead_is_visible_without_payloads(monitor):
    from argus.entrypoints.mcp.server import create_server

    await create_server(monitor.runtime).call_tool("observe", {})
    async with httpx.AsyncClient(base_url=monitor.url) as client:
        status = (await client.get("/api/status")).json()["overhead"]
        assert status["response_totals"]["responses"] == 1
        assert status["response_totals"]["result_json_bytes"] > 0
        metrics = (await client.get("/metrics")).text
        assert 'argus_response_total{measurement="responses"} 1' in metrics


async def test_monitor_sse_gap_and_disconnect_release_subscriber(monitor):
    for _ in range(600):
        monitor.runtime.events.publish("session", "idle")
    async with httpx.AsyncClient(base_url=monitor.url) as client:
        async with client.stream("GET", "/api/events", headers={"Last-Event-ID": "1"}) as response:
            async for line in response.aiter_lines():
                assert line == "event: gap"
                break
    for _ in range(20):
        if monitor.subscribers == 0:
            break
        await asyncio.sleep(0.01)
    assert monitor.subscribers == 0
    assert len(monitor.runtime.events.events) == 512


async def test_port_conflict_is_explicit_and_first_monitor_survives(monitor):
    other = Monitor(monitor.runtime, monitor.port)
    with pytest.raises(OSError):
        await other.start()
    async with httpx.AsyncClient(base_url=monitor.url) as client:
        assert (await client.get("/healthz")).status_code == 200


async def test_disconnect_before_stream_start_releases_slot(monitor):
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "query_string": b""}
    response = await monitor.events(Request(scope))
    assert monitor.subscribers == 1

    async def disconnected():
        return {"type": "http.disconnect"}

    async def broken_send(message):
        raise OSError("client left before headers")

    await response(scope, disconnected, broken_send)
    assert monitor.subscribers == 0


async def test_monitor_enforces_subscriber_limit(monitor):
    monitor.subscribers = 32
    try:
        async with httpx.AsyncClient(base_url=monitor.url) as client:
            assert (await client.get("/api/events")).status_code == 429
    finally:
        monitor.subscribers = 0


async def test_browser_dashboard_tracks_real_testing_flow(monitor):
    from playwright.async_api import async_playwright, expect

    from argus.entrypoints.mcp.server import create_server
    from argus_lab.evaluation.agent_fixture import agent_fixture

    server = create_server(monitor.runtime, "agent")
    errors = []
    with agent_fixture("profile-buggy") as (origin, events):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1120, "height": 760})
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.goto(monitor.url)
                await expect(page.locator("#connection")).to_have_text("Connected")
                await server.call_tool(
                    "start_session", {"url": origin, "goals": ["edit-name", "reload-name"]}
                )
                await server.call_tool(
                    "coverage_update",
                    {"goal": ["edit-name", "reload-name"], "status": "in_progress"},
                )
                await expect(page.locator("#goal")).to_have_text("Active goal numbers: 1, 2")
                await server.call_tool(
                    "test_form", {"form_fields": {"Display name": "Morgan"}, "submit": "Save"}
                )
                await server.call_tool(
                    "verify_persistence", {"expect": "present", "target_text": "Morgan"}
                )
                await server.call_tool(
                    "record_bug",
                    {
                        "title": "Saved name disappears after reload",
                        "severity": "high",
                        "evidence": {"description": "Saved Morgan, then fresh load restored Alex."},
                        "verify": {"expect": "absent", "target_text": "Morgan", "at_url": origin},
                    },
                )
                await server.call_tool(
                    "coverage_update",
                    {
                        "goal": ["edit-name", "reload-name"],
                        "status": "exercised",
                        "evidence": "Saved name disappeared after a fresh load.",
                    },
                )
                await expect(page.locator("#testing")).to_contain_text("Goals exercised: 2/2")
                await expect(page.locator("#testing")).to_contain_text("Checks: 1")
                await expect(page.locator("#testing")).to_contain_text("Reproduced symptoms: 1")
                body = await page.inner_text("body")
                assert "Morgan" not in body and "edit-name" not in body and origin not in body
                assert any(event["method"] == "POST" for event in events)
                assert monitor.runtime.snapshot()["testing"]["verdicts"]["reproduced"] == 1
                if output := os.environ.get("ARGUS_MONITOR_DEMO"):
                    await page.screenshot(path=output, full_page=True)
                assert not errors
                await server.call_tool("end_session", {})
                overhead = monitor.runtime.snapshot()["overhead"]
                for component in (
                    "browser_wait",
                    "observation",
                    "screenshot",
                    "report_export",
                    "response_encode",
                ):
                    assert overhead["component_calls"][component] > 0
                    assert overhead["component_seconds"][component] > 0
                assert "Morgan" not in json.dumps(overhead) and origin not in json.dumps(overhead)
            finally:
                await monitor.runtime.shutdown()
                await browser.close()
