from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from argus.application.coverage import _coverage_cursor
from argus.application.finish import end_session
from argus.domain.contracts import Rejection, SessionState


def active_session(runtime):
    session = runtime.session
    session.mode = "screen"
    session.screen = SimpleNamespace(app_name="Fixture", stop=AsyncMock())
    session.start_time = asyncio.get_running_loop().time()
    session.coverage_goals = [
        {"goal": goal, "status": "in_progress", "evidence": ""}
        for goal in ("save name", "reload name", "delete name")
    ]
    session._coverage_goal_cursors[0] = _coverage_cursor(session)
    session.coverage_action_trace.append({"tool": "test_form", "description": "Save"})
    session._coverage_goal_cursors[1] = _coverage_cursor(session)
    session.coverage_action_trace.append({"tool": "verify_persistence", "description": "Reload"})
    session.verification_checks.append({"expect": "present", "matches": False})
    runtime.transition(SessionState.ACTIVE)
    return session


async def test_final_coverage_preserves_goal_windows_and_unlisted_goals(isolated_runtime):
    session = active_session(isolated_runtime)
    session.coverage_goals[2]["status"] = "untested"
    result = await isolated_runtime.execute(
        "end_session",
        end_session,
        [
            {"goal": "save name", "status": "exercised", "evidence": "Saved, then value was lost."},
            {
                "goal": "reload name",
                "status": "blocked",
                "evidence": "Reload lost the saved value.",
            },
        ],
    )
    assert result.error is None and result.state == "idle"
    first, second, untouched = result.value.data["coverage"]["goals"]
    assert len(first["evidence_refs"]["actions"]) == 2
    assert len(second["evidence_refs"]["actions"]) == 1
    assert first["evidence_refs"]["verifications"][0]["matches"] is False
    assert untouched == {"goal": "delete name", "status": "untested", "evidence": ""}
    assert result.value.data["summary"]["tool_calls"] == 1
    exported = json.loads(
        next(isolated_runtime.settings.output_dir.glob("report_*.json")).read_text()
    )
    assert exported["coverage"]["goals"] == [first, second, untouched]
    assert not session.active


@pytest.mark.parametrize(
    "invalid",
    [
        {"goal": "unknown", "status": "exercised", "evidence": "Checked"},
        {"goal": "name", "status": "blocked", "evidence": "Ambiguous"},
        {"goal": "save name", "status": "exercised", "evidence": "Duplicate"},
        {"goal": "reload name", "status": "exercised", "evidence": ""},
        {"goal": "reload name", "status": "untested", "evidence": "Reset"},
        {"goal": "reload name", "status": "blocked", "evidence": 42},
        {"goal": 42, "status": "blocked", "evidence": "Checked"},
        {"goal": "reload name", "status": "blocked", "evidence": "x" * 1001},
        {"goal": "reload name", "status": "blocked", "evidence": "Checked", "extra": True},
        None,
    ],
)
async def test_invalid_final_batch_keeps_session_and_coverage_unchanged(isolated_runtime, invalid):
    session = active_session(isolated_runtime)
    screen = session.screen
    before = deepcopy(
        (session.coverage_goals, session._coverage_goal_cursors, session._coverage_evidence_cursor)
    )
    result = await isolated_runtime.execute(
        "end_session",
        end_session,
        [{"goal": "save name", "status": "exercised", "evidence": "Observed"}, invalid],
    )
    assert result.error.value == "invalid_argument"
    assert isolated_runtime.state == SessionState.ACTIVE
    assert session.active and not screen.stop.called
    assert (
        session.coverage_goals,
        session._coverage_goal_cursors,
        session._coverage_evidence_cursor,
    ) == before
    assert not list(isolated_runtime.settings.output_dir.glob("report_*"))


async def test_final_batch_limit_and_no_goal_session_are_rejected(isolated_runtime):
    session = active_session(isolated_runtime)
    update = {"goal": "save name", "status": "exercised", "evidence": "Observed"}
    assert isinstance(await end_session([update] * 21), Rejection)
    session.coverage_goals.clear()
    assert isinstance(await end_session([update]), Rejection)
    assert session.active


async def test_final_coverage_survives_export_failure(isolated_runtime):
    session = active_session(isolated_runtime)

    def fail(*args):
        raise OSError("Synthetic unavailable disk")

    isolated_runtime.reporter_factory = lambda: SimpleNamespace(generate=fail)
    result = await isolated_runtime.execute(
        "end_session",
        end_session,
        [{"goal": "save name", "status": "exercised", "evidence": "Saved and reloaded"}],
    )
    assert result.error is None and not session.active
    assert result.value.data["report_path"] is None
    assert result.value.data["warnings"]
    assert result.value.data["coverage"]["goals"][0]["status"] == "exercised"
