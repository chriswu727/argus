"""Screen-mode input reaches the bound app or is not sent. No real input here:
the workspace, the accessibility layer and cliclick are all stand-ins."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from argus.application.lifecycle import start_screen_session
from argus.application.screen import screen_quit
from argus.domain.contracts import ErrorCode
from argus.screen import backend as screen_backend
from argus.screen import safety
from argus.screen.backend import ScreenBackend, ScreenElement, _point, key_commands


class App:
    def __init__(self, pid, name, bundle="", activates=False, workspace=None):
        self.pid, self.name, self.bundle = pid, name, bundle
        self.activates, self.workspace = activates, workspace

    def processIdentifier(self):
        return self.pid

    def localizedName(self):
        return self.name

    def bundleIdentifier(self):
        return self.bundle

    def activateWithOptions_(self, _options):
        if self.activates:
            self.workspace.front = self


class Workspace:
    def __init__(self):
        self.apps, self.front = [], None

    def runningApplications(self):
        return self.apps

    def frontmostApplication(self):
        return self.front


@pytest.fixture
def bound(monkeypatch):
    """A backend bound to Notes (pid 7) while a terminal is frontmost."""
    workspace = Workspace()
    notes = App(7, "Notes", "com.apple.Notes", workspace=workspace)
    terminal = App(9, "Terminal", "com.apple.Terminal", workspace=workspace)
    workspace.apps, workspace.front = [notes, terminal], terminal
    backend = ScreenBackend()
    backend._workspace = workspace
    backend._app_pid, backend._app_name = 7, "Notes"
    sent = []

    def run(args, **kwargs):
        sent.append(list(args))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(screen_backend.subprocess, "run", run)
    monkeypatch.setattr(screen_backend.time, "sleep", lambda _s: None)
    return SimpleNamespace(backend=backend, notes=notes, workspace=workspace, sent=sent)


@pytest.mark.parametrize(
    "keys, commands",
    [
        (["return"], ["kp:return"]),
        (["arrow-down", "page-up"], ["kp:arrow-down", "kp:page-up"]),
        (["cmd-s"], ["kd:cmd", "t:s", "ku:cmd"]),
        (["cmd", "shift", "z"], ["kd:cmd,shift", "t:z", "ku:cmd,shift"]),
        (["tab", "cmd-delete"], ["kp:tab", "kd:cmd", "kp:delete", "ku:cmd"]),
        (["cmd+arrow-down"], ["kd:cmd", "kp:arrow-down", "ku:cmd"]),
        (["control-option-f5"], ["kd:ctrl,alt", "kp:f5", "ku:ctrl,alt"]),
    ],
)
def test_modifiers_wrap_only_their_own_key(keys, commands):
    assert key_commands(keys) == commands


@pytest.mark.parametrize("keys", [["cmd"], ["launch-rockets"], ["cmd-"], [""]])
def test_unknown_keys_are_rejected_before_anything_is_pressed(keys):
    with pytest.raises(ValueError):
        key_commands(keys)


def test_negative_coordinates_are_absolute():
    assert _point(-1150, 320) == "=-1150,320"
    assert _point(10, 20) == "10,20"


def test_input_is_not_sent_while_another_app_is_frontmost(bound):
    assert bound.backend.press_key("return")[0] is False
    assert bound.backend.click_at(10, 10)[0] is False
    assert bound.backend.type_at(10, 10, "hello")[0] is False
    assert bound.sent == []


def test_bound_app_is_brought_forward_before_input(bound):
    bound.notes.activates = True
    assert bound.backend.press_keys(["cmd-s"]) == (True, "cliclick-cmd-s")
    assert bound.sent == [["cliclick", "kd:cmd", "t:s", "ku:cmd"]]


def test_typing_needs_focus_on_the_field(bound):
    bound.workspace.front = bound.notes
    bound.backend._AX = SimpleNamespace(
        kAXValueAttribute="v",
        kAXFocusedAttribute="f",
        AXUIElementSetAttributeValue=lambda *_: -25202,
    )
    field = ScreenElement(
        role="AXTextField",
        role_description="text field",
        title="Password",
        value="",
        description="",
        enabled=True,
        focused=False,
        x=0,
        y=0,
        width=10,
        height=10,
    )
    ok, method = bound.backend.type_into(field, "S3cret")
    assert ok is False and "S3cret" not in method
    assert bound.sent == []


def test_failed_typing_does_not_echo_the_text(bound, monkeypatch):
    bound.workspace.front = bound.notes

    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr(screen_backend.subprocess, "run", run)
    ok, method = bound.backend.type_at(5, 5, "S3cret-Passw0rd!")
    assert ok is False and "S3cret" not in method


@pytest.mark.parametrize(
    "kwargs",
    [{"button": "middle"}, {"count": 5000}, {"hold_ms": 3_600_000}, {"count": 0}],
)
def test_click_arguments_are_bounded(bound, kwargs):
    bound.workspace.front = bound.notes
    assert bound.backend.click_at(1, 1, **kwargs)[0] is False
    assert bound.sent == []


def test_drag_duration_is_bounded(bound):
    bound.workspace.front = bound.notes
    assert bound.backend.drag(0, 0, 5, 5, duration_ms=86_400_000)[0] is False
    assert bound.sent == []


def test_app_lookup_needs_an_exact_or_unique_match(bound):
    bound.workspace.apps += [App(2, "Xcode", "com.apple.dt.Xcode"), App(3, "Code", "com.ms.VSCode")]
    match = bound.backend._running_app_matching
    assert match("Code").localizedName() == "Code"
    assert match("Notes").processIdentifier() == 7
    with pytest.raises(RuntimeError, match="several running apps"):
        match("com.apple")
    assert match("Safari") is None


def test_abort_file_path_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", "~/.argus/abort")
    assert safety.abort_file_path() == tmp_path / ".argus" / "abort"
    safety.prepare_abort_location()
    assert (tmp_path / ".argus").is_dir()


@pytest.mark.parametrize("raw, value", [("0", 1.0), ("-5", 1.0), ("nan", 15.0), ("9999", 120.0)])
def test_per_call_timeout_stays_usable(monkeypatch, raw, value):
    monkeypatch.setenv("ARGUS_SCREEN_PER_CALL_TIMEOUT_S", raw)
    assert safety.per_call_timeout_s() == value


async def test_a_new_session_does_not_clear_the_abort_file(isolated_runtime, monkeypatch, tmp_path):
    abort = tmp_path / "abort"
    abort.write_text("")
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(abort))
    monkeypatch.setattr("argus.screen.permissions.gate_screen_mode", lambda: [])
    result = await isolated_runtime.execute("start_screen_session", start_screen_session)
    assert result.error is ErrorCode.PERMISSION
    assert abort.exists()
    assert "screen control is stopped" in await screen_quit("Safari", force=True)
