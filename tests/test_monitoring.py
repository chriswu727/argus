from __future__ import annotations

import asyncio
import json

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
