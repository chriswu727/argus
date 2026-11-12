"""A failed or refused screen-mode action is an error with a code. No real input:
the screen backend is a stand-in and nothing is sent to the desktop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from argus.application import screen as screen_tools
from argus.domain.contracts import ErrorCode
from argus.screen import safety
from argus.screen.backend import ScreenElement


def _element(title):
    return ScreenElement(
        role="AXButton",
        role_description="button",
        title=title,
        value="",
        description="",
        enabled=True,
        focused=False,
        x=0,
        y=0,
        width=10,
        height=10,
    )


class StandInScreen:
    app_name = "Notes"
    app_pid = 7

    def __init__(self, ok=True):
        self.ok = ok
        self.pressed = []

    def press_key(self, key):
        self.pressed.append(key)
        return (True, "ax-key") if self.ok else (False, "cliclick not installed")

    def press_keys(self, keys):
        self.pressed.extend(keys)
        return (True, "ax-keys") if self.ok else (False, "cliclick not installed")


@pytest.fixture
def screen_session(isolated_runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(tmp_path / "abort"))
    session = isolated_runtime.session
    session.mode = "screen"
    session.screen = StandInScreen()
    session._safety = safety.SafetyState()
    session._last_screen_elements = [_element("Save draft"), _element("Save copy")]
    return session


async def test_a_successful_action_is_not_an_error(isolated_runtime, screen_session):
    result = await isolated_runtime.execute(
        "screen_press_key", screen_tools.screen_press_key, "return"
    )
    assert result.error is None
    assert "Pressed return" in result.value


async def test_a_failed_action_is_a_backend_error(isolated_runtime, screen_session):
    screen_session.screen.ok = False
    result = await isolated_runtime.execute(
        "screen_press_key", screen_tools.screen_press_key, "return"
    )
    assert result.error is ErrorCode.BACKEND
    assert "cliclick not installed" in result.value
    record = screen_session._safety.trail[-1]
    assert (record.tool, record.success, record.error) == (
        "screen_press_key",
        False,
        "cliclick not installed",
    )


async def test_a_timeout_is_a_timeout_error(isolated_runtime, screen_session, monkeypatch):
    async def times_out(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(safety, "with_timeout", times_out)
    result = await isolated_runtime.execute(
        "screen_press_key", screen_tools.screen_press_key, "return"
    )
    assert result.error is ErrorCode.TIMEOUT
    assert screen_session._safety.trail[-1].method == "timeout"
    assert screen_session.screen.pressed == []


async def test_the_abort_file_is_a_permission_error(isolated_runtime, screen_session, tmp_path):
    (tmp_path / "abort").write_text("")
    result = await isolated_runtime.execute("screen_keys", screen_tools.screen_keys, ["cmd-s"])
    assert result.error is ErrorCode.PERMISSION
    assert screen_session.screen.pressed == []
    assert screen_session._safety.aborted


async def test_an_aborted_session_and_the_session_cap_are_permission_errors(
    isolated_runtime, screen_session, monkeypatch
):
    screen_session._safety.aborted = True
    result = await isolated_runtime.execute("screen_keys", screen_tools.screen_keys, ["tab"])
    assert result.error is ErrorCode.PERMISSION

    screen_session._safety = safety.SafetyState(started_at=0.0)
    result = await isolated_runtime.execute("screen_keys", screen_tools.screen_keys, ["tab"])
    assert result.error is ErrorCode.PERMISSION
    assert "safety cap" in result.value
    assert screen_session.screen.pressed == []


async def test_missing_macos_grants_are_a_permission_error(isolated_runtime, monkeypatch):
    monkeypatch.setattr(
        "argus.screen.permissions.gate_screen_mode",
        lambda: [SimpleNamespace(name="Accessibility")],
    )
    result = await isolated_runtime.execute("screen_launch", screen_tools.screen_launch, "Notes")
    assert result.error is ErrorCode.PERMISSION
    assert "Accessibility" in result.value


@pytest.mark.parametrize(
    "description, fragment", [("Delete", "No AX element matches"), ("Save", "ambiguous")]
)
async def test_an_unresolved_description_is_an_invalid_argument(
    isolated_runtime, screen_session, description, fragment
):
    result = await isolated_runtime.execute(
        "screen_click_what", screen_tools.screen_click_what, description
    )
    assert result.error is ErrorCode.INVALID_ARGUMENT
    assert fragment in result.value


async def test_bad_arguments_and_the_wrong_mode_are_invalid_arguments(
    isolated_runtime, screen_session
):
    result = await isolated_runtime.execute("screen_keys", screen_tools.screen_keys, [])
    assert result.error is ErrorCode.INVALID_ARGUMENT
    result = await isolated_runtime.execute("screen_quit", screen_tools.screen_quit, "")
    assert result.error is ErrorCode.INVALID_ARGUMENT

    screen_session.mode = "web"
    result = await isolated_runtime.execute("screen_keys", screen_tools.screen_keys, ["tab"])
    assert result.error is ErrorCode.INVALID_ARGUMENT
    assert "web mode" in result.value
    assert screen_session.screen.pressed == []
