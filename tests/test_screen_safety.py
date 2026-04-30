"""Tests for argus.screen.safety."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from argus.screen import safety


def test_default_per_call_timeout_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("ARGUS_SCREEN_PER_CALL_TIMEOUT_S", raising=False)
    assert safety.per_call_timeout_s() == safety.DEFAULT_PER_CALL_TIMEOUT_S


def test_per_call_timeout_overridable_by_env(monkeypatch):
    monkeypatch.setenv("ARGUS_SCREEN_PER_CALL_TIMEOUT_S", "1.5")
    assert safety.per_call_timeout_s() == 1.5


def test_session_max_seconds_defaults_to_30_minutes(monkeypatch):
    monkeypatch.delenv("ARGUS_SCREEN_SESSION_MAX_SECONDS", raising=False)
    assert safety.session_max_seconds() == 30 * 60


def test_session_max_seconds_overridable(monkeypatch):
    monkeypatch.setenv("ARGUS_SCREEN_SESSION_MAX_SECONDS", "60")
    assert safety.session_max_seconds() == 60


def test_precheck_passes_for_fresh_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(tmp_path / "abort"))
    state = safety.SafetyState()
    assert safety.precheck(state) is None


def test_precheck_fails_when_abort_file_exists(tmp_path, monkeypatch):
    abort = tmp_path / "abort"
    abort.write_text("")
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(abort))
    state = safety.SafetyState()
    msg = safety.precheck(state)
    assert msg is not None
    assert "Abort file" in msg
    assert state.aborted is True


def test_precheck_fails_when_session_expired(monkeypatch):
    monkeypatch.setenv("ARGUS_SCREEN_SESSION_MAX_SECONDS", "0")
    state = safety.SafetyState(started_at=time.time() - 1)
    msg = safety.precheck(state)
    assert msg is not None
    assert "exceeded" in msg
    assert state.aborted is True


def test_precheck_sticks_after_first_abort(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(tmp_path / "abort"))
    state = safety.SafetyState()
    state.aborted = True
    msg = safety.precheck(state)
    assert msg is not None
    assert "aborted state" in msg


def test_record_action_appends_to_trail():
    state = safety.SafetyState()
    safety.record_action(state, "tool_a", "target_x", "method_y", success=True)
    safety.record_action(state, "tool_b", "target_z", "method_w", success=False, error="boom")
    assert state.action_count == 2
    assert len(state.trail) == 2
    assert state.trail[0].tool == "tool_a"
    assert state.trail[1].error == "boom"


def test_trail_summary_caps_at_30_entries():
    state = safety.SafetyState()
    for i in range(50):
        safety.record_action(state, f"t{i}", "x", "m", success=True)
    out = safety.trail_summary(state)
    assert "50 entries" in out
    assert "and 20 earlier" in out


@pytest.mark.asyncio
async def test_with_timeout_completes_under_budget():
    async def quick():
        await asyncio.sleep(0.01)
        return "done"
    res = await safety.with_timeout(quick(), timeout_s=1.0)
    assert res == "done"


@pytest.mark.asyncio
async def test_with_timeout_raises_when_over_budget():
    async def slow():
        await asyncio.sleep(2)
        return "never"
    with pytest.raises(asyncio.TimeoutError):
        await safety.with_timeout(slow(), timeout_s=0.1)


@pytest.mark.asyncio
async def test_with_timeout_runs_sync_callable_in_executor():
    def block():
        # Fast — should complete.
        time.sleep(0.01)
        return 42
    res = await safety.with_timeout(block, timeout_s=1.0)
    assert res == 42


def test_banner_mentions_session_cap_and_abort_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_SCREEN_SESSION_MAX_SECONDS", "120")
    monkeypatch.setenv("ARGUS_SCREEN_ABORT_FILE", str(tmp_path / "abort"))
    out = safety.banner()
    assert "120s" in out
    assert "abort" in out.lower()
    assert str(tmp_path / "abort") in out
