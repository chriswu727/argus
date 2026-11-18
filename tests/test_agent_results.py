from __future__ import annotations

import asyncio
import json

import pytest

from argus.application.payloads import observation_data
from argus.application.progress import Stage, emit, progress_scope
from argus.domain.contracts import ErrorCode, OperationResult, ToolText
from argus.entrypoints.mcp.results import AgentResult, to_mcp_result


def test_structured_result_does_not_require_parsing_legacy_text():
    value = ToolText(
        "arbitrary human wording",
        {"verification": {"matches": False}, "access_token": "SYNTHETIC_SECRET"},
    )
    with progress_scope() as progress:
        result = to_mcp_result("verify_persistence", OperationResult(value), progress)
    payload = AgentResult.model_validate(result.structuredContent)
    assert payload.data["verification"]["matches"] is False
    assert "SYNTHETIC_SECRET" not in result.model_dump_json()
    assert payload.status == "completed"
    assert result.content[0].text == "arbitrary human wording"


@pytest.mark.parametrize("code", list(ErrorCode))
def test_errors_are_structured_and_preserve_legacy_codes(code):
    with progress_scope() as progress:
        result = to_mcp_result("observe", OperationResult("Cannot continue", code), progress)
    assert result.isError
    assert result.structuredContent["error_code"] == code.value
    assert f"[{code.value}]" in result.content[0].text


def test_observation_bounds_and_count_delta(isolated_runtime, page_factory, element_factory):
    state = page_factory(
        url="https://example.test/",
        page_text="a" * 2000,
        counts={"tasks": 2},
        elements=[element_factory(text=f"button {i}") for i in range(90)],
    )
    payload = observation_data(isolated_runtime.session, state, {"tasks": 1}, state.url)
    assert len(payload["targets"]) == 80
    assert payload["omitted"]["targets"] == 10
    assert payload["omitted"]["text_characters_in_legacy_content"] == 200
    assert payload["count_changes"] == {"tasks": 1}
    assert (
        observation_data(isolated_runtime.session, state, {"tasks": 1}, "https://other.test")[
            "count_changes"
        ]
        == {}
    )


async def test_progress_failure_is_bounded_and_does_not_fail_operation(isolated_runtime):
    notifications = []

    async def blocked(sequence, message):
        notifications.append(message)
        await asyncio.Event().wait()

    async def operation():
        await isolated_runtime.progress(Stage.VERIFYING)
        return "done"

    with progress_scope(blocked) as progress:
        result = await asyncio.wait_for(isolated_runtime.execute("probe", operation), 1)
    assert result.error is None
    assert progress.delivery_failed
    assert progress.stages == ["started", "verifying", "completed"]
    assert notifications == ["Operation started"]
    assert not isolated_runtime.lock.locked()


async def test_progress_is_request_local_bounded_and_contains_no_inputs():
    async def request(stage):
        with progress_scope() as progress:
            await asyncio.sleep(0)
            for _ in range(70):
                await emit(stage)
            return progress

    first, second = await asyncio.gather(request(Stage.VERIFYING), request(Stage.OBSERVING))
    assert first.stages == ["verifying"] * 64 and first.omitted == 6
    assert second.stages == ["observing"] * 64


async def test_progress_cancellation_releases_runtime_lock(isolated_runtime):
    entered = asyncio.Event()

    async def blocked(sequence, message):
        entered.set()
        await asyncio.Event().wait()

    async def operation():
        return "not reached"

    async def request():
        with progress_scope(blocked):
            await isolated_runtime.execute("observe", operation)

    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not isolated_runtime.lock.locked()


async def test_monitor_progress_events_never_include_tool_arguments(isolated_runtime):
    async def operation(secret):
        await isolated_runtime.progress(Stage.VERIFYING)
        return secret

    await isolated_runtime.execute("probe", operation, "SYNTHETIC_PRIVATE_VALUE")
    _, events = isolated_runtime.events.read(0)
    assert "SYNTHETIC_PRIVATE_VALUE" not in json.dumps(events)
    assert any(event["kind"] == "progress" and event["code"] == "verifying" for event in events)


async def test_the_closing_stage_is_recorded_but_never_sent(isolated_runtime):
    """The response follows the closing stage at once; a notification that
    close to it can reach a client after the response and break the connection."""
    sent = []

    async def notify(sequence, message):
        sent.append(message)

    async def operation():
        await isolated_runtime.progress(Stage.VERIFYING)
        return "done"

    async def fails():
        raise RuntimeError("synthetic")

    with progress_scope(notify) as progress:
        await asyncio.wait_for(isolated_runtime.execute("probe", operation), 5)
    assert progress.stages == ["started", "verifying", "completed"]
    assert sent == [Stage.STARTED.value, Stage.VERIFYING.value]
    with progress_scope(notify) as progress:
        result = await asyncio.wait_for(isolated_runtime.execute("probe", fails), 5)
    assert result.error is not None and progress.stages == ["started", "failed"]
    assert Stage.FAILED.value not in sent and Stage.COMPLETED.value not in sent


async def test_cancel_during_a_late_notification_closes_backend(isolated_runtime):
    entered = asyncio.Event()

    class Backend:
        closed = False

        async def stop(self):
            self.closed = True

    backend = Backend()
    isolated_runtime.session.browser = backend

    async def notify(sequence, message):
        if message == Stage.EXPORTING.value:
            entered.set()
            await asyncio.Event().wait()

    async def operation():
        # The last notification an operation still sends, just before it returns.
        await isolated_runtime.progress(Stage.EXPORTING)
        return "done"

    async def request():
        with progress_scope(notify):
            await isolated_runtime.execute("observe", operation)

    task = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.closed
    assert isolated_runtime.state.value == "failed"
    assert isolated_runtime.last_error == "cancelled"
    assert not isolated_runtime.lock.locked()
