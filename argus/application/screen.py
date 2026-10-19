from __future__ import annotations

import asyncio
from typing import Optional

from argus.application.context import current_runtime
from argus.domain.models import (
    Screenshot,
)
from argus.resolver import (
    describe_screen as describe_screen_element,
)
from argus.resolver import (
    resolve_screen_element,
)

from .evidence import _auto_screenshot, _screenshot_dir
from .session import Session, _require_session


def _format_screen_observation(obs) -> str:
    """Render a ScreenObservation into the same vibe as web-mode observe()."""
    lines = [
        f"App: {obs.foreground_app}  (pid {obs.foreground_pid})",
        f"Window: {obs.foreground_window_title!r}",
        f"Screen: {obs.screen_width}x{obs.screen_height}",
        f"Screenshot: {obs.screenshot_path or '(capture failed)'}",
        "",
        "Interactive elements (AX tree, capped):",
    ]
    if not obs.elements:
        lines.append("  (none — the app may be unresponsive or AX-blind)")
    else:
        for el in obs.elements:
            label = el.title or el.value or el.description or el.role_description or el.role
            bits = [el.role]
            if label:
                bits.append(f'"{label[:60]}"')
            if el.path and len(el.path) > 1:
                # Last 1-2 ancestors give useful disambiguating context.
                ctx = " / ".join(p for p in el.path[-2:] if p)[:60]
                if ctx:
                    bits.append(f"(in: {ctx})")
            bits.append(f"@ ({el.x},{el.y}) {el.width}x{el.height}")
            if not el.enabled:
                bits.append("[disabled]")
            if el.focused:
                bits.append("[focused]")
            lines.append(f"  - {' '.join(bits)}")
    lines.append("")
    lines.append(
        "Argus does not auto-judge content quality on screen mode either. "
        "Decide what's a bug from this output and call record_bug."
    )
    return "\n".join(lines)


def _require_screen_session() -> Session:
    """Like _require_session, but also rejects web-mode + runs safety
    precheck. Returns the session if all gates pass; raises or returns
    a string-based error message via the caller-side pattern."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        raise RuntimeError(
            "This tool is screen-mode only. Call start_screen_session() "
            "or use the equivalent web-mode tool."
        )
    return s


def _safety_or_error(s: Session) -> Optional[str]:
    """Run the screen-mode safety pre-check, returning an error string
    if the next action should be refused, or None to proceed."""
    from argus.screen import safety as screen_safety

    if s._safety is None:
        return "Screen-mode session has no safety state — start_screen_session() first."
    return screen_safety.precheck(s._safety)


async def _observe_screen(s: Session) -> str:
    err = _safety_or_error(s)
    if err:
        return err

    from argus.screen import safety as screen_safety

    try:
        obs = await screen_safety.with_timeout(
            s.screen.observe(screenshot_dir=str(_screenshot_dir(s)))
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_observe",
            "",
            "timeout",
            success=False,
            error="AX/observe timed out",
        )
        return (
            "screen_observe: AX query timed out. The target app may be "
            "unresponsive or AX-blind. Try `screen_observe()` again, or "
            "switch target_app via start_screen_session()."
        )
    s._last_screen_elements = obs.elements
    if obs.screenshot_path:
        s._screenshot_counter += 1
        s.screenshots.append(
            Screenshot(
                path=obs.screenshot_path,
                name=f"screen_{s._screenshot_counter:03d}",
                step="screen_observe",
                url=f"screen://{obs.foreground_app}",
            )
        )
    screen_safety.record_action(
        s._safety,
        "screen_observe",
        obs.foreground_app,
        "ok",
        success=True,
        post_screenshot=obs.screenshot_path,
    )
    return _format_screen_observation(obs)


async def screen_observe() -> str:
    """Re-snapshot the screen — fresh screenshot + fresh AX tree of the
    foreground (or target) app.

    Same role as observe() but for screen mode. Returns the foreground
    app name, the focused window's title, the AX-tree elements with
    screen coordinates, and the path to a fresh screenshot.

    Argus does not auto-flag UX issues here. The screenshot is yours
    to look at; the AX tree is yours to reason about; call record_bug
    when you've confirmed something real.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_observe: this session is in web mode "
            f"(mode={s.mode!r}). End this session and call "
            "start_screen_session() to switch."
        )
    return await _observe_screen(s)


def _resolve_screen_or_error(s, description: str, kind_filter=None, *, strict_kind: bool = False):
    """Resolve a description against the cached AX-tree elements.

    Mirrors `_resolve_or_error` for screen mode. Returns
    (element, None) on success or (None, error_message) when the
    description doesn't pin down a single element. AX-blind apps
    (Unity / Electron with custom rendering / Adobe self-render /
    web-canvas tools) get a specific hint pointing at the coordinate
    escape hatches — read the screenshot, identify (x, y), call
    screen_click_at / screen_type_at instead.
    """
    elements = s._last_screen_elements

    # AX-blind: the target app exposes nothing useful in its AX tree
    # (or the agent never observed). Surface the coordinate fallback
    # explicitly — this is the most common screen-mode failure mode
    # and the user shouldn't have to discover the workaround on their own.
    if not elements or len(elements) <= 1:
        return None, (
            "screen_click_what / screen_type_into can't help here — the "
            "target app exposes "
            f"{len(elements)} interactive AX element(s). It's effectively "
            "AX-blind (Unity, custom-rendered Electron, Adobe self-render, "
            "web-canvas tools all behave this way).\n\n"
            "Use the coordinate escape hatch:\n"
            "  1. Re-screenshot if needed (screen_observe captures a fresh PNG)\n"
            "  2. Read the screenshot yourself, identify the (x, y) of the\n"
            "     target element on the visible window\n"
            "  3. Call screen_click_at(x, y), screen_drag(...), screen_type_at(x, y, text),\n"
            "     screen_keys([...]), or screen_hover_at(x, y) as appropriate"
        )

    result = resolve_screen_element(
        description,
        elements,
        kind_filter=kind_filter,
        strict_kind=strict_kind,
    )
    if result.reason == "unique" and result.found is not None:
        return result.found, None
    if result.reason == "no_match":
        return None, (
            f"No AX element matches {description!r}. Call screen_observe() "
            f"to see what's actually exposed; the AX tree changes when the "
            f"foreground app changes or new windows open. If this app "
            f"exposes nothing useful in AX (Unity, custom Electron, etc.), "
            f"fall back to screen_click_at(x, y) with coordinates from the "
            f"screenshot."
        )
    lines = [f"Description {description!r} is ambiguous in screen mode:"]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_screen_element(el)}")
    lines.append(
        "\nPick a more specific description (use the AX role, the "
        "containing window/section, or unique substring of the label)."
    )
    return None, "\n".join(lines)


async def screen_click_what(description: str) -> str:
    """Click a screen element matched by natural-language description.

    Resolves against the most-recent screen_observe() snapshot. Tries
    the AX press action first (atomic, no mouse hijack), falls back to
    a coordinate click via cliclick at the element's centre. After the
    click, you'll typically want to call screen_observe() to see what
    changed.

    Example: screen_click_what("Save button"),
             screen_click_what("Cancel in the Confirm dialog")
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_click_what: this session is in web mode "
            "(use click_what for web). End and start a screen session first."
        )
    err = _safety_or_error(s)
    if err:
        return err
    el, err = _resolve_screen_or_error(s, description)
    if err:
        return err

    from argus.screen import safety as screen_safety

    label = el.title or el.value or el.description or el.role
    s.steps.append(f"screen_click_what({description!r}) -> {el.role} {label[:40]!r}")

    pre_path = await _auto_screenshot(
        s,
        "screen_click_pre",
        f"pre-click {description!r}",
    )

    try:
        ok, method = await screen_safety.with_timeout(lambda: s.screen.click(el))
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_click_what",
            description,
            "timeout",
            success=False,
            pre_screenshot=pre_path,
            error="click timed out",
        )
        return f"screen_click_what({description!r}) — click timed out."

    post_path = await _auto_screenshot(
        s,
        "screen_click_post",
        f"post-click {description!r}",
    )
    screen_safety.record_action(
        s._safety,
        "screen_click_what",
        description,
        method,
        success=ok,
        pre_screenshot=pre_path,
        post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return (
            f"screen_click_what({description!r}) — click failed via {method}.\n"
            f"The element may not implement the press action and cliclick "
            f"isn't installed (brew install cliclick), or the element moved."
        )
    return (
        f'Clicked {el.role} "{label[:60]}" via {method}.\n'
        f"  pre-screenshot: {pre_path}\n"
        f"  post-screenshot: {post_path}\n"
        f"Call screen_observe() to see what changed."
    )


async def screen_type_into(description: str, text: str) -> str:
    """Type `text` into a screen text field matched by description.

    Tries setting AXValue directly first (cleanest for native text
    controls). If refused, focuses the element and synthesises
    keystrokes via cliclick.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_type_into: this session is in web mode (use type_into for web)."
    err = _safety_or_error(s)
    if err:
        return err
    el, err = _resolve_screen_or_error(
        s,
        description,
        kind_filter="input",
        strict_kind=True,
    )
    if err:
        return err

    from argus.screen import safety as screen_safety

    label = el.title or el.value or el.description or el.role
    s.steps.append(f"screen_type_into({description!r}, ...) -> {label[:40]!r}")

    pre_path = await _auto_screenshot(s, "screen_type_pre", f"pre-type into {label[:30]}")
    try:
        ok, method = await screen_safety.with_timeout(lambda: s.screen.type_into(el, text))
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_type_into",
            description,
            "timeout",
            success=False,
            pre_screenshot=pre_path,
            error="type timed out",
        )
        return f"screen_type_into({description!r}) — typing timed out."
    post_path = await _auto_screenshot(s, "screen_type_post", f"post-type into {label[:30]}")
    screen_safety.record_action(
        s._safety,
        "screen_type_into",
        description,
        method,
        success=ok,
        pre_screenshot=pre_path,
        post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return (
            f"screen_type_into({description!r}) — typing failed via {method}.\n"
            f"The element may be read-only, or cliclick isn't installed."
        )
    return f'Typed into "{label[:60]}" via {method}.'


async def screen_press_key(key: str) -> str:
    """Press a single key. Pass a cliclick key name: 'return', 'esc',
    'space', 'tab', 'arrow-up', 'cmd-s' (combined), etc.

    Useful for: submitting a focused form (return), dismissing a modal
    (esc), navigating menus (arrow keys), keyboard shortcuts (cmd-s).
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_press_key: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from argus.screen import safety as screen_safety

    s.steps.append(f"screen_press_key({key!r})")
    try:
        ok, method = await screen_safety.with_timeout(lambda: s.screen.press_key(key))
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_press_key",
            key,
            "timeout",
            success=False,
            error="key press timed out",
        )
        return f"screen_press_key({key!r}) timed out."
    screen_safety.record_action(
        s._safety,
        "screen_press_key",
        key,
        method,
        success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_press_key({key!r}) failed: {method}"
    return f"Pressed {key} via {method}."


async def screen_click_at(
    x: int,
    y: int,
    button: str = "left",
    count: int = 1,
    hold_ms: int = 0,
) -> str:
    """Click at absolute screen coordinates — the escape hatch for
    AX-blind apps (Unity, custom-rendered Electron, Adobe self-render,
    web-canvas tools). Use this when screen_click_what reports an empty
    AX tree.

    Workflow: read the most recent screenshot, identify the (x, y) of
    the element you want to click, call this tool. Re-screenshot or
    screen_observe afterwards to see what changed.

    Args:
        x, y: absolute screen coordinates (the same coordinate space
              as screen_observe's element rects).
        button: "left" (default), "right", or "middle".
        count: 1 (single, default), 2 (double-click), 3 (triple), or
               N for rapid consecutive clicks (race-condition probing).
        hold_ms: when > 0, press-and-hold for that many milliseconds
                 before releasing.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_click_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from argus.screen import safety as screen_safety

    target = f"({x},{y}) {button} x{count}" + (f" hold={hold_ms}ms" if hold_ms else "")
    s.steps.append(f"screen_click_at({target})")

    pre_path = await _auto_screenshot(s, "screen_click_at_pre", f"pre-click {target}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.click_at(x, y, button=button, count=count, hold_ms=hold_ms),
            timeout_s=max(5.0, hold_ms / 1000 + 5.0),
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_click_at",
            target,
            "timeout",
            success=False,
            pre_screenshot=pre_path,
            error="click timed out",
        )
        return f"screen_click_at({target}) timed out."
    post_path = await _auto_screenshot(s, "screen_click_at_post", f"post-click {target}")
    screen_safety.record_action(
        s._safety,
        "screen_click_at",
        target,
        method,
        success=ok,
        pre_screenshot=pre_path,
        post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_click_at({target}) failed: {method}"
    return (
        f"Clicked at ({x},{y}) via {method}.\n"
        f"  pre:  {pre_path}\n  post: {post_path}\n"
        f"Re-observe (screen_observe) or read the post-screenshot to see what changed."
    )


async def screen_hover_at(x: int, y: int) -> str:
    """Move the cursor to (x, y) without clicking. Use to surface
    hover-state styling on a specific element (the agent then re-
    screenshots to see the hover effect)."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_hover_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    from argus.screen import safety as screen_safety

    s.steps.append(f"screen_hover_at({x},{y})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.hover_at(x, y),
        )
    except asyncio.TimeoutError:
        return "screen_hover_at timed out."
    screen_safety.record_action(
        s._safety,
        "screen_hover_at",
        f"({x},{y})",
        method,
        success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_hover_at failed: {method}"
    return f"Cursor at ({x},{y}) via {method}."


async def screen_drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration_ms: int = 300,
) -> str:
    """Press at (from_x, from_y), move to (to_x, to_y), release.

    Required for sliders, kanban-style reordering, dragging files,
    drawing strokes, and any app that distinguishes drag from click.
    Default duration is 300 ms because some apps drop zero-duration
    drag events as accidental clicks.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_drag: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from argus.screen import safety as screen_safety

    target = f"({from_x},{from_y})->({to_x},{to_y}) {duration_ms}ms"
    s.steps.append(f"screen_drag({target})")

    pre_path = await _auto_screenshot(s, "screen_drag_pre", f"pre-drag {target}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.drag(from_x, from_y, to_x, to_y, duration_ms=duration_ms),
            timeout_s=max(5.0, duration_ms / 1000 + 5.0),
        )
    except asyncio.TimeoutError:
        return f"screen_drag({target}) timed out."
    post_path = await _auto_screenshot(s, "screen_drag_post", f"post-drag {target}")
    screen_safety.record_action(
        s._safety,
        "screen_drag",
        target,
        method,
        success=ok,
        pre_screenshot=pre_path,
        post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_drag failed: {method}"
    return f"Dragged {target} via {method}.\n  pre:  {pre_path}\n  post: {post_path}"


async def screen_keys(keys: list) -> str:
    """Press a sequence of keys in order — the multi-key version of
    screen_press_key. Each item is a cliclick key name (`return`,
    `esc`, `space`, `tab`, `arrow-up`, …) or a combo (`cmd-s`,
    `cmd-shift-z`). Useful for keyboard-only navigation flows
    (`["arrow-down", "arrow-down", "return"]`) and OS-level shortcuts
    (`["cmd-tab"]`).
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_keys: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    if not isinstance(keys, list) or not keys:
        return "screen_keys: pass a non-empty list of cliclick key names."

    from argus.screen import safety as screen_safety

    s.steps.append(f"screen_keys({keys})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.press_keys(keys),
            timeout_s=max(5.0, 0.3 * len(keys) + 3.0),
        )
    except asyncio.TimeoutError:
        return "screen_keys timed out."
    screen_safety.record_action(
        s._safety,
        "screen_keys",
        str(keys),
        method,
        success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_keys failed: {method}"
    return f"Pressed {keys} via {method}."


async def screen_type_at(x: int, y: int, text: str) -> str:
    """Click at (x, y) to focus, then type `text`. The coordinate-
    based escape hatch for AX-blind text fields (where set-AXValue
    is unavailable)."""
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_type_at: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    from argus.screen import safety as screen_safety

    s.steps.append(f"screen_type_at({x},{y}, {len(text)} chars)")
    pre_path = await _auto_screenshot(s, "screen_type_at_pre", f"pre-type at ({x},{y})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.type_at(x, y, text),
            timeout_s=max(10.0, 0.05 * len(text) + 5.0),
        )
    except asyncio.TimeoutError:
        return "screen_type_at timed out."
    post_path = await _auto_screenshot(s, "screen_type_at_post", f"post-type at ({x},{y})")
    screen_safety.record_action(
        s._safety,
        "screen_type_at",
        f"({x},{y})",
        method,
        success=ok,
        pre_screenshot=pre_path,
        post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_type_at failed: {method}"
    return f"Typed {len(text)} chars at ({x},{y}) via {method}.\n  pre: {pre_path}\n  post: {post_path}"


async def screen_wait_for_stable(
    timeout_s: float = 5.0,
    threshold_pct: float = 0.5,
    stable_window_ms: int = 400,
    poll_ms: int = 150,
) -> str:
    """Wait until the target window stops changing visually.

    Use this between an action and the observation that follows it.
    After clicking "New Game", the screen is in motion: loading
    spinner, scene transition, animation. Sleeping for an arbitrary
    N ms is brittle. This tool polls a screenshot every `poll_ms` and
    returns once `stable_window_ms` worth of consecutive frames stay
    below `threshold_pct` pixel difference. If `timeout_s` fires first
    you get a clear "timeout" verdict with the last frame so you can
    still inspect what the agent saw.

    Args:
        timeout_s: hard cap on wall-clock waiting.
        threshold_pct: per-frame pixel-difference threshold for "stable"
                       (0.5 means 0.5% of pixels can flicker).
        stable_window_ms: how long the page must stay below threshold
                          before we declare it settled.
        poll_ms: how often to take a fresh screenshot.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_wait_for_stable: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err

    from argus.screen import safety as screen_safety

    target = (
        f"timeout={timeout_s}s threshold={threshold_pct}% "
        f"stable={stable_window_ms}ms poll={poll_ms}ms"
    )
    s.steps.append(f"screen_wait_for_stable({target})")

    try:
        settled, reason, final_path, stats = await screen_safety.with_timeout(
            lambda: s.screen.wait_for_stable(
                timeout_s=timeout_s,
                threshold_pct=threshold_pct,
                stable_window_ms=stable_window_ms,
                poll_ms=poll_ms,
                screenshot_dir=str(_screenshot_dir(s)),
            ),
            # Allow the wait itself to hit its own internal timeout, then
            # add a small grace before the safety wrapper trips.
            timeout_s=timeout_s + 5.0,
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety,
            "screen_wait_for_stable",
            target,
            "outer-timeout",
            success=False,
            error="outer asyncio timeout fired",
        )
        return "screen_wait_for_stable hit the outer safety timeout."

    screen_safety.record_action(
        s._safety,
        "screen_wait_for_stable",
        target,
        f"settled={settled}/{reason}",
        success=settled,
        post_screenshot=final_path,
        error=None if settled else reason,
    )
    if final_path:
        s._screenshot_counter += 1
        s.screenshots.append(
            Screenshot(
                path=final_path,
                name=f"screen_wait_{s._screenshot_counter:03d}",
                step=f"wait_for_stable: {reason}",
                url=f"screen://{s.screen.app_name or 'unknown'}",
            )
        )

    if settled:
        return (
            f"Settled after {stats.get('frames', '?')} frames "
            f"(last diff {stats.get('last_diff_pct', '?')}%).\n"
            f"  final: {final_path}"
        )
    if reason == "timeout":
        return (
            f"screen_wait_for_stable: timed out after {timeout_s}s, "
            f"page still moving (last diff {stats.get('last_diff_pct', '?')}%, "
            f"stable streak {stats.get('stable_for_ms', 0)}ms / "
            f"required {stable_window_ms}ms).\n"
            f"  final: {final_path}\n"
            f"  Either bump timeout_s, lower stable_window_ms, or accept that "
            f"this surface is intentionally animated."
        )
    return f"screen_wait_for_stable: {reason}.\n  final: {final_path}"


def _adhoc_screen_backend():
    """Build a one-off ScreenBackend for lifecycle calls that don't need
    an active session (launch / quit / is_running can run before a
    session is started or against a different target than the bound one)."""
    from argus.screen.backend import ScreenBackend

    backend = ScreenBackend()
    backend._load_frameworks()
    return backend


async def screen_launch(target: str, wait_s: float = 8.0) -> str:
    """Launch a macOS app by localised name, bundle id, or absolute path.

    If the app is already running, returns the existing pid without
    re-launching. Otherwise shells out to `open -a <target>` and polls
    until the new process appears or `wait_s` elapses.

    This unlocks save-persistence testing: launch the app fresh, drive
    a flow, screen_quit it, screen_launch again, observe whether state
    survived.
    """
    if not target:
        return "screen_launch: target is required (app name / bundle id / path)."
    try:
        from argus.screen.permissions import gate_screen_mode
    except ImportError as exc:
        return f"screen_launch: screen-mode deps missing — pip install argus-testing[mac]. ({exc})"
    missing = gate_screen_mode()
    if missing:
        names = ", ".join(c.name for c in missing)
        return f"screen_launch: missing macOS grants: {names}. Run argus-mcp --doctor."

    try:
        backend = _adhoc_screen_backend()
        ok, method, pid = backend.launch(target, wait_s=wait_s)
    except Exception as exc:
        return f"screen_launch failed: {exc}"
    if not ok:
        return f"screen_launch({target!r}): {method}"

    # If a screen session is currently bound to the same app and we just
    # re-launched it, the old AX refs are stale — force the session to
    # rebind on next observe.
    s = current_runtime().session
    if s.active and s.mode == "screen" and s.screen is not None:
        s.screen.invalidate_target()  # forces _find_target_app to re-resolve
    return f"Launched {target!r} (pid {pid}, {method})."


async def screen_quit(target: str, force: bool = False, wait_s: float = 8.0) -> str:
    """Quit a macOS app gracefully (like cmd-Q) or forcibly (SIGKILL).

    Use the polite path by default — apps need the chance to flush
    state if you're testing save-on-quit. Pass `force=True` only when
    the app is hung or you're explicitly testing crash recovery.
    """
    if not target:
        return "screen_quit: target is required."
    try:
        backend = _adhoc_screen_backend()
        ok, method = backend.quit(target, force=force, wait_s=wait_s)
    except Exception as exc:
        return f"screen_quit failed: {exc}"

    # If the current session was bound to the app we just killed, the
    # bound state is no longer valid — the agent should call
    # start_screen_session again.
    s = current_runtime().session
    if (
        s.active
        and s.mode == "screen"
        and s.screen is not None
        and s.screen.app_name
        and target.lower() in s.screen.app_name.lower()
    ):
        s.screen.invalidate_target()

    if not ok:
        return f"screen_quit({target!r}): {method}"
    return f"Quit {target!r} via {method}."


async def screen_is_running(target: str) -> str:
    """Check whether a macOS app is currently running. Returns the pid
    if so. Useful as a wait/poll primitive after screen_launch /
    screen_quit, or to verify a relaunch actually replaced the old
    process."""
    if not target:
        return "screen_is_running: target is required."
    try:
        backend = _adhoc_screen_backend()
        running, pid = backend.is_running(target)
    except Exception as exc:
        return f"screen_is_running failed: {exc}"
    if running:
        return f"{target!r} is running (pid {pid})."
    return f"{target!r} is not running."


async def screen_screenshot_region(
    x: int,
    y: int,
    width: int,
    height: int,
    name: str = "region",
) -> str:
    """Capture a rectangular region of the screen — for reading fine
    detail on a specific surface.

    Coordinates are absolute screen coords (the same space the agent
    sees in screen_observe / AX-tree element rects). VLMs are markedly
    more accurate on tight crops than full-window screenshots — when
    you need to read tiny error-toast text or distinguish two similar
    icons, capture just that region.

    Args:
        x, y: top-left corner of the region.
        width, height: pixel dimensions. Must be > 0.
        name: filename label.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_screenshot_region: this session is in web mode."
    err = _safety_or_error(s)
    if err:
        return err
    if width <= 0 or height <= 0:
        return f"screen_screenshot_region: invalid dimensions {width}x{height}."

    s.steps.append(f"screen_screenshot_region({x},{y},{width}x{height})")
    out_dir = str(_screenshot_dir(s))
    path = s.screen.capture_region(x, y, width, height, screenshot_dir=out_dir)
    if path is None:
        return (
            f"screen_screenshot_region: capture failed. "
            f"Check that ({x},{y}) + {width}x{height} stays inside the screen "
            f"({s.screen.app_pid and 'screen size from start_screen_session output'})."
        )
    s._screenshot_counter += 1
    s.screenshots.append(
        Screenshot(
            path=path,
            name=f"region_{s._screenshot_counter:03d}_{name}",
            step=f"region@({x},{y}) {width}x{height}",
            url=f"screen://{s.screen.app_name or 'unknown'}",
        )
    )
    return f"Region screenshot saved: {path}\n  rect: ({x},{y}) {width}x{height}"


async def screen_session_status() -> str:
    """Show how the current screen-mode session is doing: time used vs
    session cap, action count, abort-file state, recent action trail.

    Useful between steps to verify you're not about to hit the session
    cap, and after the fact to review what Argus did.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_session_status: this session is in web mode."

    import time as _time

    from argus.screen import safety as screen_safety

    state = s._safety
    if state is None:
        return "screen_session_status: no safety state — bug in session bootstrap."

    elapsed = int(_time.time() - state.started_at)
    remaining = int(screen_safety.session_remaining_seconds(state))
    abort_present = screen_safety.abort_file_present()

    lines = [
        "Screen session status:",
        f"  elapsed: {elapsed}s",
        f"  remaining (cap): {remaining}s",
        f"  action count: {state.action_count}",
        f"  aborted: {state.aborted}",
        f"  abort file present: {abort_present} ({screen_safety.abort_file_path()})",
        "",
        screen_safety.trail_summary(state),
    ]
    return "\n".join(lines)
