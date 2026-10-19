from __future__ import annotations

import asyncio
from typing import List, Optional
from urllib.parse import urlparse

from argus.application.context import current_runtime
from argus.domain.contracts import ErrorCode, OperationError, Rejection, SessionState

from .coverage import _contract_items, _session_protocol, _update_coverage_from_state
from .evidence import _journal_entries, _screenshot_dir
from .observation import _coverage_line, _format_observation
from .session import Session, _teardown_active_session


async def start_session(
    url: str,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 720,
    include_observation: bool = True,
    review_mode: str = "exploratory",
    goals: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    time_budget_minutes: int = 0,
) -> str:
    """Start a browser testing session and navigate to the given URL.

    Args:
        url: The URL to test (e.g. http://localhost:3000)
        headless: Run browser without visible window (default True)
        viewport_width: Browser viewport width in pixels
        viewport_height: Browser viewport height in pixels
        include_observation: Return the initial page observation in this call.
        review_mode: exploratory, visual, or regression.
        goals: Up to 20 user journeys or review outcomes to exercise.
        constraints: Up to 20 user-supplied boundaries to preserve in the report.
        time_budget_minutes: Advisory budget from 1 to 240 minutes, or 0 for none.
    """

    review_mode = review_mode.strip().lower()
    if review_mode not in {"exploratory", "visual", "regression"}:
        return Rejection("start_session: review_mode must be exploratory, visual, or regression.")
    cleaned_goals, error = _contract_items("goals", goals)
    if error:
        return Rejection(error)
    cleaned_constraints, error = _contract_items("constraints", constraints)
    if error:
        return Rejection(error)
    if time_budget_minutes < 0 or time_budget_minutes > 240:
        return Rejection("start_session: time_budget_minutes must be 0 or between 1 and 240.")

    if not 1 <= viewport_width <= 16384 or not 1 <= viewport_height <= 16384:
        return Rejection("start_session: viewport dimensions must be between 1 and 16384.")

    teardown_errors = await _teardown_active_session()
    if teardown_errors:
        current_runtime().transition(SessionState.FAILED)
        raise OperationError(
            ErrorCode.BACKEND,
            "could not close the active session safely — " + "; ".join(teardown_errors),
        )

    current_runtime().session = new_session = Session()
    current_runtime().transition(SessionState.STARTING)
    new_session.mode = "web"
    new_session.review_mode = review_mode
    new_session.url = url
    new_session.start_time = asyncio.get_event_loop().time()
    new_session.coverage_goals = [
        {"goal": goal, "status": "untested", "evidence": ""} for goal in cleaned_goals
    ]
    new_session.focus_areas = list(cleaned_goals)
    new_session.constraints = cleaned_constraints
    new_session.time_budget_minutes = time_budget_minutes
    new_session.browser = current_runtime().browser_factory(
        headless=headless,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    try:
        await new_session.browser.start()
    except Exception as exc:
        await current_runtime().close_backends()
        current_runtime().transition(SessionState.FAILED)
        raise OperationError(
            ErrorCode.BACKEND,
            f"failed to launch the browser — "
            f"{type(exc).__name__}: {str(exc)[:160]}\n"
            f"Run `playwright install chromium` and try again.",
        )
    try:
        await new_session.browser.goto(url)
    except Exception as exc:
        # Don't leak the Playwright stack-trace; classify the common shapes.
        msg = str(exc)
        hint = ""
        if "ERR_CONNECTION_REFUSED" in msg:
            hint = " The host is up but nothing is listening on that port."
        elif "ERR_NAME_NOT_RESOLVED" in msg or "getaddrinfo" in msg:
            hint = " The hostname couldn't be resolved — check the URL."
        elif "Timeout" in msg or "timeout" in msg:
            hint = " The page took longer than 30 s to settle to networkidle."
        # Tear down the half-built browser before bubbling the error.
        await current_runtime().close_backends()
        current_runtime().transition(SessionState.FAILED)
        raise OperationError(
            ErrorCode.BACKEND,
            f"could not load {url} — {type(exc).__name__}.{hint}\nNo session was created.",
        )

    current_runtime().session = new_session
    current_runtime().session.tool_calls = 1
    current_runtime().session.pages_visited.append(url)

    state = await current_runtime().session.browser.get_state()
    current_runtime().session._last_elements = state.elements
    current_runtime().session._last_observed_counts = dict(state.counts)
    current_runtime().session._last_observed_url = state.url
    element_count = len(state.elements)
    _update_coverage_from_state(current_runtime().session, state)

    hint = ""
    n_journaled = len(_journal_entries(urlparse(url).netloc or "default"))
    if n_journaled:
        hint = (
            f"\n{n_journaled} finding(s) from prior runs are journaled for this site — "
            "call regression_check() to re-test whether they're fixed or back."
        )

    summary = (
        f"Web session started.\n"
        f"Page: {state.title}\n"
        f"URL: {state.url}\n"
        f"Review mode: {review_mode}\n"
        f"Found {element_count} interactive elements.{hint}"
    )
    introduction = summary + "\n\n" + _session_protocol(current_runtime().session)
    if not include_observation:
        return (
            introduction
            + _coverage_line(current_runtime().session, state)
            + "\nCall observe() to inspect the page."
        )
    return (
        introduction
        + "\n\nInitial observation:\n"
        + _format_observation(state)
        + _coverage_line(current_runtime().session, state)
    )


async def start_screen_session(target_app: str = "") -> str:
    """Start a macOS screen-mode testing session.

    Argus will probe the user's actual screen — whatever app is foreground
    (or `target_app` if given) becomes the system under test. Use this
    when the thing you're testing is not a web app, or when you want to
    test a web app through its real browser chrome rather than a headless
    Playwright instance.

    Requires Screen Recording and Accessibility grants. Run
    `argus-mcp --doctor` first if you haven't.

    Args:
        target_app: Localised app name (e.g. "Safari", "Notes", "Cursor").
                    If empty, binds to whatever is foreground at the
                    moment of the call.
    """

    try:
        from argus.screen import safety as screen_safety
        from argus.screen.backend import ScreenBackend
        from argus.screen.permissions import gate_screen_mode
    except ImportError as exc:
        return (
            f"start_screen_session: screen-mode dependencies not installed.\n"
            f"  pip install argus-testing[mac]\n"
            f"  ({exc})"
        )

    missing = gate_screen_mode()
    if missing:
        lines = ["start_screen_session: missing macOS grants:"]
        for c in missing:
            lines.append(f"  - {c.name}: {c.detail}")
            lines.append(f"    Open: {c.settings_url}")
        lines.append("")
        lines.append("Run `argus-mcp --doctor` for full details, then re-try.")
        return "\n".join(lines)

    # Stale abort file from a previous session would gate every action
    # immediately — clean it up at the boundary.
    abort_path = screen_safety.abort_file_path()
    if abort_path.exists():
        try:
            abort_path.unlink()
        except OSError:
            pass

    teardown_errors = await _teardown_active_session()
    if teardown_errors:
        return "start_screen_session: could not close the active session safely — " + "; ".join(
            teardown_errors
        )

    current_runtime().session = Session()
    current_runtime().transition(SessionState.STARTING)
    current_runtime().session.mode = "screen"
    current_runtime().session.start_time = asyncio.get_event_loop().time()
    current_runtime().session.screen = ScreenBackend()
    current_runtime().session._safety = screen_safety.SafetyState()
    try:
        obs = await screen_safety.with_timeout(
            current_runtime().session.screen.start(
                target_app=target_app or None,
                screenshot_dir=str(_screenshot_dir(current_runtime().session)),
            )
        )
    except asyncio.TimeoutError:
        await current_runtime().close_backends()
        current_runtime().transition(SessionState.FAILED)
        raise OperationError(
            ErrorCode.TIMEOUT,
            "AX query timed out — the target app "
            "may be unresponsive or AX-blind. Try again, or pass an "
            "explicit `target_app` to bind to a specific known-good app.",
        )
    except Exception as exc:
        await current_runtime().close_backends()
        current_runtime().transition(SessionState.FAILED)
        raise OperationError(ErrorCode.BACKEND, f"failed to start — {type(exc).__name__}") from exc

    current_runtime().session._last_screen_elements = obs.elements
    current_runtime().session.tool_calls = 1

    # Banner to stderr so a user running argus-mcp from a terminal sees
    # the warning. Silently no-ops in MCP-over-stdio if stderr is not
    # captured by the host.
    import sys as _sys

    print(screen_safety.banner(), file=_sys.stderr)

    return (
        f"Screen session started.\n"
        f"Foreground app: {obs.foreground_app} (pid {obs.foreground_pid})\n"
        f"Window: {obs.foreground_window_title!r}\n"
        f"Screen: {obs.screen_width}x{obs.screen_height}\n"
        f"AX-tree elements: {len(obs.elements)}\n"
        f"Initial screenshot: {obs.screenshot_path}\n"
        f"\n"
        f"Safety:\n"
        f"  Session cap: {int(screen_safety.session_max_seconds())}s\n"
        f"  Per-call timeout: {screen_safety.per_call_timeout_s()}s\n"
        f"  Abort file: `touch {screen_safety.abort_file_path()}` to stop\n"
        f"\n"
        f"Call screen_observe() to see what's on screen."
    )
