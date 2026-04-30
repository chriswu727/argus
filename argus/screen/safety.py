"""Screen-mode safety scaffolding.

Screen mode lets a smart agent drive the user's actual mouse and
keyboard on the user's actual machine. That is qualitatively riskier
than a headless web browser. This module provides:

- a per-call timeout so a hung AX query doesn't lock up the agent,
- a session-age cap so a runaway agent can't drive the screen
  indefinitely (default 30 min; override with ARGUS_SCREEN_SESSION_MAX_SECONDS),
- an abort file that, when present, blocks every further screen-mode
  action until the next session starts (default
  ~/.argus/abort; override with ARGUS_SCREEN_ABORT_FILE),
- a structured action log so the user can review what Argus did after
  the fact.

These checks fire in the MCP-tool layer rather than deep in the
backend so the user-facing error messages stay actionable.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

T = TypeVar("T")

# Sensible defaults. Both can be overridden via env so the user can
# loosen them for "go to lunch, run for an hour" use without editing code.
DEFAULT_PER_CALL_TIMEOUT_S = 15.0
DEFAULT_SESSION_MAX_SECONDS = 30 * 60  # 30 minutes
DEFAULT_ABORT_FILE = str(Path.home() / ".argus" / "abort")


@dataclass
class ActionRecord:
    """One entry in the action trail."""
    timestamp: float
    tool: str
    target: str
    method: str  # "ax-press" | "cliclick-coord:x,y" | etc.
    success: bool
    pre_screenshot: Optional[str] = None
    post_screenshot: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SafetyState:
    """Per-session safety state. Lives on the Session object."""
    started_at: float = field(default_factory=time.time)
    action_count: int = 0
    aborted: bool = False
    trail: List[ActionRecord] = field(default_factory=list)


def per_call_timeout_s() -> float:
    raw = os.environ.get("ARGUS_SCREEN_PER_CALL_TIMEOUT_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_PER_CALL_TIMEOUT_S


def session_max_seconds() -> float:
    raw = os.environ.get("ARGUS_SCREEN_SESSION_MAX_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_SESSION_MAX_SECONDS


def abort_file_path() -> Path:
    return Path(os.environ.get("ARGUS_SCREEN_ABORT_FILE", DEFAULT_ABORT_FILE))


def abort_file_present() -> bool:
    return abort_file_path().exists()


def session_expired(state: SafetyState) -> bool:
    return (time.time() - state.started_at) > session_max_seconds()


def session_remaining_seconds(state: SafetyState) -> float:
    return max(0.0, session_max_seconds() - (time.time() - state.started_at))


async def with_timeout(
    coro_or_callable: Awaitable[T] | Callable[[], T],
    *,
    timeout_s: Optional[float] = None,
) -> T:
    """Run an async coroutine OR a sync callable with a timeout.

    The sync path runs the callable in the default executor — useful
    for AX / cliclick calls that block the event loop otherwise.
    """
    timeout = timeout_s if timeout_s is not None else per_call_timeout_s()

    if asyncio.iscoroutine(coro_or_callable):
        return await asyncio.wait_for(coro_or_callable, timeout=timeout)

    if callable(coro_or_callable):
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, coro_or_callable),
            timeout=timeout,
        )

    # Already a value — just return it.
    return coro_or_callable  # type: ignore[return-value]


def precheck(state: SafetyState) -> Optional[str]:
    """Return a reason-string if the next action should be refused, or
    None if it's safe to proceed.

    Encodes the safety order: abort file > session expired > already
    aborted in this session.
    """
    if state.aborted:
        return (
            "Argus screen-mode session is in aborted state. "
            "Start a new session with start_screen_session() to continue."
        )
    if abort_file_present():
        state.aborted = True
        return (
            f"Abort file detected at {abort_file_path()} — refusing all "
            f"further screen actions in this session. Remove the file "
            f"and start a new session to resume."
        )
    if session_expired(state):
        state.aborted = True
        cap = session_max_seconds()
        return (
            f"Screen-mode session exceeded the {cap:.0f}s safety cap. "
            f"Start a new session to continue (override with "
            f"ARGUS_SCREEN_SESSION_MAX_SECONDS)."
        )
    return None


def record_action(
    state: SafetyState,
    tool: str,
    target: str,
    method: str,
    success: bool,
    pre_screenshot: Optional[str] = None,
    post_screenshot: Optional[str] = None,
    error: Optional[str] = None,
) -> ActionRecord:
    state.action_count += 1
    record = ActionRecord(
        timestamp=time.time(),
        tool=tool,
        target=target,
        method=method,
        success=success,
        pre_screenshot=pre_screenshot,
        post_screenshot=post_screenshot,
        error=error,
    )
    state.trail.append(record)
    return record


def banner() -> str:
    """Short banner the user sees on session start. Goes to stderr."""
    cap = int(session_max_seconds())
    abort = abort_file_path()
    return (
        "[ARGUS] Screen mode active — Argus may move your mouse and keyboard.\n"
        f"[ARGUS]   Session cap: {cap}s.\n"
        f"[ARGUS]   Abort: `touch {abort}` to stop further screen actions.\n"
    )


def trail_summary(state: SafetyState) -> str:
    lines = [f"Action trail ({len(state.trail)} entries):"]
    for r in state.trail[-30:]:
        mark = "[ok]" if r.success else "[fail]"
        ts = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
        lines.append(
            f"  {ts} {mark} {r.tool}({r.target[:40]!r}) via {r.method[:40]}"
        )
        if r.error:
            lines.append(f"    error: {r.error[:120]}")
    if len(state.trail) > 30:
        lines.append(f"  ...and {len(state.trail) - 30} earlier entries")
    return "\n".join(lines)
