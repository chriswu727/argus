"""Argus MCP Server — exposes browser testing tools to Claude Code.

Claude Code becomes the AI planner. Argus provides the browser, DOM
extraction, error capture, and report generation. No API key needed.

Usage in Claude Code settings (~/.claude/settings.json):
    {
        "mcpServers": {
            "argus": {
                "command": "argus-mcp"
            }
        }
    }

Then in Claude Code just say:
    "Test my app at http://localhost:3000, focus on the login flow"
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from .browser import BrowserDriver
from .detector import Detector
from .differ import compute_changes
from .models import Bug, BugType, ExplorationResult, InteractiveElement, PageState, Screenshot, Severity
from .reporter import Reporter
from .resolver import (
    describe as describe_element,
    describe_screen as describe_screen_element,
    resolve_element,
    resolve_screen_element,
)

mcp = FastMCP(
    "argus",
    instructions="""You are now Argus, the all-seeing QA tester for a software product.
While this MCP is loaded you are not a coding assistant, not a task
completer, not the user's friend. You are a senior human QA tester sitting
down at the user's machine with one job: find the bugs the dev team would
be embarrassed to ship. Stay in role until end_session is called.

GOAL
Quality of bugs found matters more than quantity. A tight 5-bug report
beats a noisy 50-bug report. Ship findings a real user would care about,
not theoretical ones.

NOT YOUR JOB
- Completing the user's flow as if you were a real user (don't actually
  buy the thing, don't actually publish the post — unless the act itself
  is what you're testing).
- Suggesting code fixes — you are QA, not dev.
- SEO / Lighthouse-style performance audits / generic accessibility
  scans — those are different products. Use axe / Lighthouse for those.
  You only flag a11y or perf when it makes the app unusable for a real user.
- Mechanically firing every payload from a security textbook. A real
  tester picks one well-chosen probe per surface, observes, moves on.

THE TESTER'S RITUAL — return to this on every tool call

1. MAP         What does this app let a user do? Identify the 3-5 user
               goals before testing anything specific.
2. HYPOTHESIZE For each surface, name 2-3 specific ways it could fail.
               Not "the form might break" — "I bet validation runs only
               client-side and the server accepts garbage".
3. ACT         One probe per tool call. Resist testing five things in
               one click.
4. OBSERVE     After every action, read what came back: state diff,
               console, network, visible feedback. Compare expected vs
               actual. Take a screenshot when something looks off.
5. VERIFY      For any destructive or persistence-changing action
               (delete, save, edit, submit, toggle, payment), call
               verify_persistence. UIs lie. The "Saved!" toast is the
               single most common reason real users lose data.
6. RECORD      When you've confirmed a real bug, call record_bug with
               severity + reproducible steps + evidence (URL, element,
               screenshot index). Don't record speculation. Don't record
               polish nits.
7. COVER       Before ending the session, ask "which user goals did I
               never exercise?" — go test those.

WHAT MAKES A REAL BUG (the bar)
- Reproducible — someone following your steps will see it too.
- User-affecting — causes data loss, security risk, blocked flow, real
  confusion, or trust damage.
- Persistent — not a one-off page-load race unless you can re-trigger it.

THE THINGS HUMANS NOTICE THAT MACHINES MISS — your hunting ground
- The success toast is a lie — operation didn't actually persist.
- Cross-page state inconsistency — same datum displayed differently
  across pages, or one page updates and another doesn't.
- Empty states aren't designed (says "Loading..." forever, or just blank).
- Long values silently truncated with no indication.
- Validation messages are engineer-speak, not user-speak ("Field 'foo'
  invalid" — what is foo, what should I do?).
- A workflow has no back / cancel / recover path — user is trapped.
- Visual hierarchy inverted — destructive button is the prominent one;
  primary CTA is the dim one.
- Dark patterns — fake urgency, hidden cost, hard-to-cancel, deceptive
  consent, sneaky charges.
- After auth, navigation/UI doesn't reflect logged-in state.
- Form errors clear the user's input — they have to retype everything.
- The same action via two paths gives different results.
- Inputs accept what should be rejected (auth bypass, bypassed
  validation, accepted out-of-range numbers, accepted whitespace where
  content is required).

SEVERITY CALIBRATION
- HIGH    data loss, security, payment, blocked primary flow
- MEDIUM  workflow friction, confusing UX, deceptive feedback,
          cross-page inconsistency
- LOW     polish, copy, suggestion-grade

OPERATING RHYTHM
After every tool call, ask yourself: was that a tester move, or did I
slip into being a regular user / a developer / a wandering agent? If
you slipped, return to the ritual. The MCP is loaded specifically so
you stay in the tester seat — use that.""",
)


class Session:
    """Holds the state for one testing session.

    A session has a `mode` of either "web" (BrowserDriver) or "screen"
    (ScreenBackend). Tools that span both modes (record_bug,
    end_session) check `mode`; tools that are mode-specific (observe
    vs screen_observe) live as separate tools so the contract stays
    honest about what each mode can do.
    """

    def __init__(self):
        self.mode: Optional[str] = None  # "web" | "screen" | None
        self.browser: Optional[BrowserDriver] = None
        self.screen = None  # type: Optional["ScreenBackend"]
        self.detector = Detector()
        self.bugs: List[Bug] = []
        self.steps: List[str] = []
        self.pages_visited: List[str] = []
        self.screenshots: List[Screenshot] = []
        self.start_time: Optional[float] = None
        self.url: Optional[str] = None
        self.focus_areas: List[str] = []
        self._last_elements = []
        self._last_screen_elements = []
        self._screenshot_counter = 0
        # Lazy-initialised screen-mode safety state. Populated by
        # start_screen_session; consulted by every screen-mode tool.
        self._safety = None  # type: Optional["argus.screen.safety.SafetyState"]

    @property
    def active(self) -> bool:
        return self.browser is not None or self.screen is not None


_session = Session()


def _require_session() -> Session:
    if not _session.active:
        raise RuntimeError(
            "No active session. Call start_session(url) for web mode "
            "or start_screen_session() for screen mode first."
        )
    return _session


def _output_dir() -> str:
    return os.environ.get("ARGUS_OUTPUT_DIR", "./argus-reports")


async def _auto_screenshot(s: Session, name: str, step: str) -> str:
    """Take a screenshot and register it in the session.

    Mode-aware: web-mode goes through Playwright; screen-mode shells
    out to `screencapture`. Both produce a PNG at the same output
    location and append a Screenshot record with the correct URL.
    """
    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")

    if s.mode == "web" and s.browser is not None:
        await s.browser.screenshot(path)
        url = s.browser._page.url if s.browser._page else ""
    elif s.mode == "screen" and s.screen is not None:
        # Shell out for the same convention used elsewhere.
        import subprocess as _sp
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            _sp.run(["screencapture", "-t", "png", "-x", path], capture_output=True, timeout=5, check=True)
        except Exception:
            pass
        url = f"screen://{s.screen._app_name or 'unknown'}"
    else:
        url = ""

    s.screenshots.append(Screenshot(
        path=path, name=safe_name, step=step, url=url,
    ))
    return path


def _text_in_state(text: str, state: PageState) -> bool:
    """Check if text exists anywhere in page — page_text, elements, or item_lists."""
    text_lower = text.lower().strip()
    if not text_lower:
        return False
    if text_lower in state.page_text.lower():
        return True
    for el in state.elements:
        if el.text and text_lower in el.text.lower():
            return True
        if el.value and text_lower in el.value.lower():
            return True
    for items in state.item_lists.values():
        for item in items:
            if text_lower in item.lower():
                return True
    return False


async def _capture_browser_events(
    s: Session, state: PageState, console_errs: list, network_errs: list
) -> list:
    """Capture browser-side events the agent cannot see directly.

    Console messages and HTTP-layer 4xx/5xx do not surface in page state —
    they only appear via Playwright event listeners. We turn those into Bug
    records so they show up in the session report. Everything else (page
    text, counts, CSS state, toasts) is the agent's job to interpret.
    """
    bugs = s.detector.process_console_errors(console_errs, state.url, s.steps)
    bugs.extend(s.detector.process_network_errors(network_errs, state.url, s.steps))
    return bugs


@mcp.tool()
async def start_session(
    url: str,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> str:
    """Start a browser testing session and navigate to the given URL.

    Args:
        url: The URL to test (e.g. http://localhost:3000)
        headless: Run browser without visible window (default True)
        viewport_width: Browser viewport width in pixels
        viewport_height: Browser viewport height in pixels
    """
    global _session

    if _session.active:
        await _session.browser.stop()

    _session = Session()
    _session.mode = "web"
    _session.url = url
    _session.start_time = asyncio.get_event_loop().time()
    _session.browser = BrowserDriver(
        headless=headless,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    await _session.browser.start()
    await _session.browser.goto(url)
    _session.pages_visited.append(url)

    state = await _session.browser.get_state()
    _session._last_elements = state.elements
    element_count = len(state.elements)

    return (
        f"Web session started.\n"
        f"Page: {state.title}\n"
        f"URL: {state.url}\n"
        f"Found {element_count} interactive elements. "
        f"Call observe() to see them."
    )


@mcp.tool()
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
    global _session

    try:
        from .screen.permissions import gate_screen_mode
        from .screen.backend import ScreenBackend
        from .screen import safety as screen_safety
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

    if _session.active:
        if _session.browser is not None:
            await _session.browser.stop()
        if _session.screen is not None:
            await _session.screen.stop()

    _session = Session()
    _session.mode = "screen"
    _session.start_time = asyncio.get_event_loop().time()
    _session.screen = ScreenBackend()
    _session._safety = screen_safety.SafetyState()
    try:
        obs = await screen_safety.with_timeout(
            _session.screen.start(target_app=target_app or None)
        )
    except asyncio.TimeoutError:
        return (
            "start_screen_session: AX query timed out — the target app "
            "may be unresponsive or AX-blind. Try again, or pass an "
            "explicit `target_app` to bind to a specific known-good app."
        )
    except Exception as exc:
        return f"start_screen_session: failed to start — {exc}"

    _session._last_screen_elements = obs.elements

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


def _format_observation(state: PageState) -> str:
    """Render a PageState into a description-keyed observation report.

    No `[N]` indices — the agent refers to elements by what they are
    (text / role / placeholder), not by integer position. This matches
    how a human tester thinks about a screen.
    """
    lines = [f"URL: {state.url}", f"Title: {state.title}"]

    if state.page_text:
        lines.append("")
        lines.append("Page text:")
        lines.append(state.page_text[:1800])

    lines.append("")
    lines.append("Interactive elements:")
    if not state.elements:
        lines.append("  (none visible)")
    else:
        for el in state.elements:
            lines.append(f"  - {describe_element(el)}")

    if state.toast_messages:
        lines.append("")
        lines.append("Visible feedback / notifications:")
        for toast in state.toast_messages:
            lines.append(f"  [feedback] {toast}")

    if state.counts:
        lines.append("")
        lines.append("Displayed counts:")
        for label, val in state.counts.items():
            lines.append(f"  {val} {label}")

    if state.css_indicators:
        lines.append("")
        lines.append("CSS state indicators:")
        for ind in state.css_indicators:
            lines.append(f"  .{ind}")

    if state.item_lists:
        lines.append("")
        lines.append("Repeating item lists:")
        for key, items in state.item_lists.items():
            lines.append(f"  {key[:40]}: {len(items)} item(s)")
            for item in items[:3]:
                lines.append(f"    - {item[:120]}")
            if len(items) > 3:
                lines.append(f"    ... ({len(items) - 3} more)")

    if state.open_modals:
        lines.append("")
        lines.append("Open modals / dialogs / popovers:")
        for modal in state.open_modals:
            label = modal.get("ariaLabel") or modal.get("role") or "dialog"
            preview = (modal.get("text") or "").strip()[:120]
            modal_marker = " [aria-modal]" if modal.get("isModal") else ""
            lines.append(f"  - {label}{modal_marker}: {preview}")

    if state.focused:
        f = state.focused
        bits = [f.get("tag", "?")]
        for key in ("ariaLabel", "text", "placeholder", "name", "id"):
            v = f.get(key)
            if v:
                bits.append(f'{key}={v[:40]!r}')
                break
        lines.append("")
        lines.append(f"Focused element: {' '.join(bits)}")

    if state.viewport:
        v = state.viewport
        lines.append("")
        lines.append(
            f"Viewport: {v.get('innerWidth')}x{v.get('innerHeight')} | "
            f"scrollY={v.get('scrollY')} of {v.get('documentHeight')} "
            f"(at_top={v.get('atTop')}, at_bottom={v.get('atBottom')})"
        )
        if not v.get("atBottom") and v.get("documentHeight", 0) > v.get("innerHeight", 0) + 50:
            lines.append(
                "  Note: more content below the fold — scroll_down to reveal it."
            )

    lines.append("")
    lines.append(
        "To interact: click_what(\"description\"), type_into(\"field\", \"text\"), "
        "select_into(\"dropdown\", \"value\"). For deeper inspection of one element "
        "(computed styles / outerHTML / truncation), use inspect_element."
    )
    return "\n".join(lines)


def _resolve_or_error(
    s: Session,
    description: str,
    kind_filter: Optional[str] = None,
    *,
    strict_kind: bool = False,
) -> tuple[Optional[InteractiveElement], Optional[str]]:
    """Resolve a description to a single element or return an error string.

    Returns (element, None) on success, (None, error_message) on
    no_match / ambiguous / no_elements.

    Set `strict_kind=True` from tools that *cannot* gracefully handle a
    cross-kind match (e.g. type_into pointed at a link gives the user
    a Playwright stack-trace). With strict_kind, the resolver refuses
    to fall back to the full element pool.
    """
    result = resolve_element(
        description, s._last_elements,
        kind_filter=kind_filter, strict_kind=strict_kind,
    )

    if result.reason == "unique" and result.found is not None:
        return result.found, None

    if result.reason == "no_elements":
        return None, (
            "No interactive elements visible. Call observe() first, or scroll_down "
            "if you expect content below the fold."
        )

    if result.reason == "no_match":
        return None, (
            f"No element matches {description!r}. Call observe() to see what's "
            f"actually on the page, or rephrase your description."
        )

    # ambiguous
    lines = [
        f"Description {description!r} is ambiguous — multiple matches:"
    ]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_element(el)}")
    lines.append("")
    lines.append(
        "Pick a more specific description (mention surrounding text, kind hint "
        "like 'button' / 'field', or which section of the page)."
    )
    return None, "\n".join(lines)


@mcp.tool()
async def observe() -> str:
    """Observe the current target — page, app, or screen. Read this first.

    Returns the URL/window, the visible text, every interactive element
    keyed by description (no integer indices), feedback messages, counts,
    and any list-shaped repeating content. After every action, observe()
    again and reason about what changed before acting.

    The agent decides what's a bug from this output. Argus does not
    auto-flag content quality, validation behaviour, or visual issues
    here — that's your judgment to make.
    """
    s = _require_session()
    state = await s.browser.get_state()
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)
    return _format_observation(state)


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
    from .screen import safety as screen_safety
    if s._safety is None:
        return "Screen-mode session has no safety state — start_screen_session() first."
    return screen_safety.precheck(s._safety)


@mcp.tool()
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
    err = _safety_or_error(s)
    if err:
        return err

    from .screen import safety as screen_safety
    try:
        obs = await screen_safety.with_timeout(s.screen.observe())
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_observe", "", "timeout", success=False,
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
        s.screenshots.append(Screenshot(
            path=obs.screenshot_path,
            name=f"screen_{s._screenshot_counter:03d}",
            step="screen_observe",
            url=f"screen://{obs.foreground_app}",
        ))
    screen_safety.record_action(
        s._safety, "screen_observe", obs.foreground_app, "ok", success=True,
        post_screenshot=obs.screenshot_path,
    )
    return _format_screen_observation(obs)


def _resolve_screen_or_error(s, description: str, kind_filter=None, *, strict_kind: bool = False):
    """Resolve a description against the cached AX-tree elements.

    Mirrors `_resolve_or_error` for screen mode. Returns
    (element, None) on success or (None, error_message) when the
    description doesn't pin down a single element.
    """
    result = resolve_screen_element(
        description, s._last_screen_elements,
        kind_filter=kind_filter, strict_kind=strict_kind,
    )
    if result.reason == "unique" and result.found is not None:
        return result.found, None
    if result.reason == "no_elements":
        return None, (
            "screen_observe() first — Argus has no AX snapshot yet."
        )
    if result.reason == "no_match":
        return None, (
            f"No AX element matches {description!r}. Call screen_observe() "
            f"to see what's actually exposed; the AX tree changes when the "
            f"foreground app changes or new windows open."
        )
    lines = [f"Description {description!r} is ambiguous in screen mode:"]
    for score, el in result.candidates:
        lines.append(f"  ({score}) {describe_screen_element(el)}")
    lines.append(
        "\nPick a more specific description (use the AX role, the "
        "containing window/section, or unique substring of the label)."
    )
    return None, "\n".join(lines)


@mcp.tool()
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

    from .screen import safety as screen_safety
    label = el.title or el.value or el.description or el.role
    s.steps.append(f'screen_click_what({description!r}) -> {el.role} {label[:40]!r}')

    pre_path = await _auto_screenshot(
        s, "screen_click_pre", f"pre-click {description!r}",
    )

    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.click(el)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_click_what", description, "timeout",
            success=False, pre_screenshot=pre_path, error="click timed out",
        )
        return f"screen_click_what({description!r}) — click timed out."

    post_path = await _auto_screenshot(
        s, "screen_click_post", f"post-click {description!r}",
    )
    screen_safety.record_action(
        s._safety, "screen_click_what", description, method,
        success=ok, pre_screenshot=pre_path, post_screenshot=post_path,
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


@mcp.tool()
async def screen_type_into(description: str, text: str) -> str:
    """Type `text` into a screen text field matched by description.

    Tries setting AXValue directly first (cleanest for native text
    controls). If refused, focuses the element and synthesises
    keystrokes via cliclick.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return (
            "screen_type_into: this session is in web mode "
            "(use type_into for web)."
        )
    err = _safety_or_error(s)
    if err:
        return err
    el, err = _resolve_screen_or_error(
        s, description, kind_filter="input", strict_kind=True,
    )
    if err:
        return err

    from .screen import safety as screen_safety
    label = el.title or el.value or el.description or el.role
    s.steps.append(f'screen_type_into({description!r}, ...) -> {label[:40]!r}')

    pre_path = await _auto_screenshot(s, "screen_type_pre", f"pre-type into {label[:30]}")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.type_into(el, text)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_type_into", description, "timeout",
            success=False, pre_screenshot=pre_path, error="type timed out",
        )
        return f"screen_type_into({description!r}) — typing timed out."
    post_path = await _auto_screenshot(s, "screen_type_post", f"post-type into {label[:30]}")
    screen_safety.record_action(
        s._safety, "screen_type_into", description, method,
        success=ok, pre_screenshot=pre_path, post_screenshot=post_path,
        error=None if ok else method,
    )
    if not ok:
        return (
            f"screen_type_into({description!r}) — typing failed via {method}.\n"
            f"The element may be read-only, or cliclick isn't installed."
        )
    return f'Typed into "{label[:60]}" via {method}.'


@mcp.tool()
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

    from .screen import safety as screen_safety
    s.steps.append(f"screen_press_key({key!r})")
    try:
        ok, method = await screen_safety.with_timeout(
            lambda: s.screen.press_key(key)
        )
    except asyncio.TimeoutError:
        screen_safety.record_action(
            s._safety, "screen_press_key", key, "timeout",
            success=False, error="key press timed out",
        )
        return f"screen_press_key({key!r}) timed out."
    screen_safety.record_action(
        s._safety, "screen_press_key", key, method, success=ok,
        error=None if ok else method,
    )
    if not ok:
        return f"screen_press_key({key!r}) failed: {method}"
    return f"Pressed {key} via {method}."


@mcp.tool()
async def screen_session_status() -> str:
    """Show how the current screen-mode session is doing: time used vs
    session cap, action count, abort-file state, recent action trail.

    Useful between steps to verify you're not about to hit the session
    cap, and after the fact to review what Argus did.
    """
    s = _require_session()
    if s.mode != "screen" or s.screen is None:
        return "screen_session_status: this session is in web mode."

    from .screen import safety as screen_safety
    import time as _time
    state = s._safety
    if state is None:
        return "screen_session_status: no safety state — bug in session bootstrap."

    elapsed = int(_time.time() - state.started_at)
    remaining = int(screen_safety.session_remaining_seconds(state))
    abort_present = screen_safety.abort_file_present()

    lines = [
        f"Screen session status:",
        f"  elapsed: {elapsed}s",
        f"  remaining (cap): {remaining}s",
        f"  action count: {state.action_count}",
        f"  aborted: {state.aborted}",
        f"  abort file present: {abort_present} ({screen_safety.abort_file_path()})",
        "",
        screen_safety.trail_summary(state),
    ]
    return "\n".join(lines)


@mcp.tool()
async def click_what(description: str) -> str:
    """Click the element best matching the natural-language `description`.

    Examples: "Login button", "Add Task", "the email field", "Delete near
    Buy groceries". Argus matches against visible text, aria-label,
    placeholder, name, id, and the parent context. Trailing kind hints
    ("button" / "field" / "link" / "dropdown") narrow the candidate pool.

    If the description is ambiguous, this returns the top candidates with
    their distinguishing properties so you can rephrase. It does not
    guess and click — that's how testers misclick.
    """
    s = _require_session()
    el, err = _resolve_or_error(s, description)
    if err:
        return err

    label = el.text or el.aria_label or el.placeholder or el.name or el.id or el.tag
    step = f'click_what({description!r}) -> "{label[:60]}"'
    s.steps.append(step)

    selector = s.browser._build_selector(el)
    try:
        await s.browser._page.click(selector, timeout=5000)
        await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as exc:
        return (
            f'click_what({description!r}) — failed to click "{label[:60]}": {exc}\n'
            "The element may be obscured, stale, or removed. Try observe() again."
        )

    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    if new_state.url not in s.pages_visited:
        s.pages_visited.append(new_state.url)
    return (
        f'Clicked "{label[:60]}" (via description {description!r}).\n'
        f"Now on: {new_state.url} — {len(new_state.elements)} interactive elements visible.\n"
        f"Call observe() to see what changed."
    )


@mcp.tool()
async def type_into(description: str, text: str) -> str:
    """Type `text` into the input element best matching `description`.

    Examples: type_into("email", "alice@x.com"), type_into("confirm
    password", "...") , type_into("the search box", "buy"). Resolution
    rules are the same as click_what — see that tool for ambiguity behaviour.
    """
    s = _require_session()
    el, err = _resolve_or_error(
        s, description, kind_filter="input", strict_kind=True,
    )
    if err:
        return err

    label = el.placeholder or el.name or el.aria_label or el.id or el.tag
    s.steps.append(f'type_into({description!r}, ...) -> {label[:60]}')

    selector = s.browser._build_selector(el)
    try:
        await s.browser._page.fill(selector, text, timeout=5000)
    except Exception as exc:
        return (
            f"type_into({description!r}) — failed: {type(exc).__name__}. "
            f"The element may be disabled or the page may have re-rendered."
        )
    return f'Typed into "{label[:60]}" (via description {description!r}).'


@mcp.tool()
async def select_into(description: str, value: str) -> str:
    """Select `value` in the dropdown best matching `description`."""
    s = _require_session()
    el, err = _resolve_or_error(
        s, description, kind_filter="select", strict_kind=True,
    )
    if err:
        return err

    label = el.aria_label or el.name or el.id or el.tag
    s.steps.append(f'select_into({description!r}, {value!r}) -> {label[:60]}')

    selector = s.browser._build_selector(el)
    try:
        await s.browser._page.select_option(selector, value, timeout=5000)
    except Exception as exc:
        return (
            f"select_into({description!r}, {value!r}) — failed: "
            f"{type(exc).__name__}. Make sure the dropdown actually has "
            f"that option (call inspect_element to list the choices)."
        )
    return f'Selected "{value}" in "{label[:60]}".'


@mcp.tool()
async def inspect_element(description: str) -> str:
    """Get computed styles, ARIA metadata, and outerHTML for one element.

    Use this when you suspect a visual / a11y / truncation bug on a
    specific surface and observe()'s summary doesn't tell you enough.
    Returns:
      - rendered styles (color, background, font-size/weight, display,
        visibility, opacity, position, z-index, overflow, cursor, etc.)
      - bounding rect + whether it's in the viewport
      - whether the element is visually truncated by CSS (scrollWidth >
        clientWidth with overflow: hidden / text-overflow: ellipsis)
      - aria-label / aria-describedby / aria-hidden / role / title
      - associated <label> text(s)
      - disabled / readonly / focused state
      - first 1.5 KB of outerHTML

    Argus does not auto-judge anything from this output. You read it
    and decide whether anything you see warrants record_bug.
    """
    s = _require_session()
    el, err = _resolve_or_error(s, description)
    if err:
        return err

    selector = s.browser._build_selector(el)
    info = await s.browser.inspect_element(selector)
    if not info.get("found"):
        return (
            f"inspect_element({description!r}) — could not re-locate element "
            f"via selector {selector!r}. The DOM may have changed; observe() again."
        )

    s.steps.append(f'inspect_element({description!r})')

    lines = [f"Inspecting {description!r} (resolved to <{info['tag']}>)"]
    lines.append("")
    lines.append("Visible text: " + (info.get("text") or "<none>")[:160])
    rect = info.get("rect", {})
    lines.append(
        f"Rect: x={rect.get('x', 0):.0f} y={rect.get('y', 0):.0f} "
        f"w={rect.get('width', 0):.0f} h={rect.get('height', 0):.0f} "
        f"in_viewport={rect.get('inViewport')}"
    )
    if info.get("truncated"):
        sd = info.get("scrollDimensions", {})
        lines.append(
            f"  TRUNCATED: scrollWidth={sd.get('scrollWidth')} > clientWidth={sd.get('clientWidth')} "
            f"(or scrollHeight > clientHeight) with overflow hidden — text is silently cut off."
        )
    if info.get("focused"):
        lines.append("Focus: this element currently has focus.")

    lines.append("")
    lines.append("Computed styles:")
    for k, v in (info.get("styles") or {}).items():
        lines.append(f"  {k}: {v}")

    lines.append("")
    lines.append("Accessibility:")
    lines.append(f"  role: {info.get('role') or '(default)'}")
    lines.append(f"  aria-label: {info.get('ariaLabel') or '(none)'}")
    lines.append(f"  aria-describedby: {info.get('ariaDescribedby') or '(none)'}")
    lines.append(f"  aria-hidden: {info.get('ariaHidden') or '(false/unset)'}")
    lines.append(f"  title: {info.get('title') or '(none)'}")
    lines.append(f"  disabled: {info.get('disabled')}, readonly: {info.get('readonly')}")
    if info.get("labels"):
        lines.append(f"  associated <label>(s): {info['labels']}")

    lines.append("")
    lines.append("outerHTML (first 1500 chars):")
    lines.append(info.get("outerHtml") or "(unavailable)")
    return "\n".join(lines)


@mcp.tool()
async def eval_js(code: str) -> str:
    """Run arbitrary JavaScript in the page context and return the result.

    Disabled by default because it can read cookies, mutate state, and
    issue any fetch the page is allowed to. Enable with the `--unsafe`
    flag at server start (or `ARGUS_UNSAFE_EVAL=1`). Use this when:
      - inspecting state the standard tools don't expose (window.X,
        a global config object, IndexedDB contents);
      - resetting or seeding a test fixture via its own internal
        endpoints (`fetch('/api/test/reset', {method: 'POST'})`);
      - probing edge cases that require triggering JS the UI doesn't
        expose (race conditions, double-submit by direct fetch).

    The tool returns the JSON-serialised return value of the JS
    expression. If your code returns an unserialisable object, wrap
    it (e.g. `JSON.stringify(...)` or pluck specific fields).

    Argus does not auto-record bugs from eval_js output. If you find
    something via eval_js, call record_bug like with anything else.

    Args:
        code: A JS expression or arrow function. Examples:
            "() => window.location.href"
            "() => fetch('/api/test/reset', {method:'POST'}).then(r=>r.status)"
            "() => Object.keys(window.appConfig || {})"
    """
    if os.environ.get("ARGUS_UNSAFE_EVAL") != "1":
        return (
            "eval_js is disabled. Restart argus-mcp with the --unsafe flag "
            "(or set ARGUS_UNSAFE_EVAL=1) to enable arbitrary JS execution. "
            "It is off by default because the JS you run has the same "
            "powers as the page itself."
        )

    s = _require_session()
    s.steps.append(f"eval_js: {code[:80]}")

    try:
        result = await s.browser._page.evaluate(code)
    except Exception as exc:
        return f"eval_js failed: {exc}"

    # Serialise. Playwright already gives us Python primitives for JSON-able
    # results. For other shapes, fall back to repr.
    try:
        import json as _json
        rendered = _json.dumps(result, default=str)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "... [truncated]"
        return f"eval_js result: {rendered}"
    except Exception:
        return f"eval_js result (repr): {result!r}"


@mcp.tool()
async def navigate(url: str) -> str:
    """Navigate to a specific URL.

    Args:
        url: The URL to navigate to
    """
    s = _require_session()
    step = f"Navigate to {url}"
    s.steps.append(step)

    await s.browser.goto(url)
    state = await s.browser.get_state()
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)

    return f"Navigated to {state.url} — {state.title} ({len(state.elements)} elements)"


@mcp.tool()
async def go_back() -> str:
    """Go back to the previous page."""
    s = _require_session()
    s.steps.append("Go back")

    ok = await s.browser.go_back()
    if ok:
        state = await s.browser.get_state()
        s._last_elements = state.elements
        return f"Went back to {state.url}"
    return "Failed to go back"


@mcp.tool()
async def scroll_down() -> str:
    """Scroll the page down to reveal more content."""
    s = _require_session()
    s.steps.append("Scroll down")
    await s.browser.scroll_down()
    return "Scrolled down. Call observe() to see what's now in view."


@mcp.tool()
async def screenshot(
    name: str = "screenshot",
    element: str = "",
    full_page: bool = False,
) -> str:
    """Capture a screenshot — full viewport, full page, or one element.

    Use this whenever something looks visually off and you want evidence
    for a record_bug call, or when you want a before/after pair to feed
    into screenshot_diff.

    Args:
        name: Filename label (no extension).
        element: Optional element description (same syntax as click_what).
                 If given, crops the screenshot to that element's bounds.
                 Use for visual hierarchy / truncation / contrast checks.
        full_page: If True, capture the entire scrollable page rather
                   than just the viewport. Ignored when `element` is set.
    """
    s = _require_session()
    last_step = s.steps[-1] if s.steps else "Initial state"

    if element:
        el, err = _resolve_or_error(s, element)
        if err:
            return err
        s._screenshot_counter += 1
        safe_name = f"{s._screenshot_counter:03d}_elem_{name}"
        path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")
        selector = s.browser._build_selector(el)
        result = await s.browser.element_screenshot(selector, path)
        if result is None:
            return (
                f"screenshot(element={element!r}) — could not capture; the "
                f"element may have moved or detached. observe() and try again."
            )
        s.screenshots.append(Screenshot(
            path=path, name=safe_name, step=last_step,
            url=s.browser._page.url if s.browser._page else "",
        ))
        return f"Element screenshot saved: {path}"

    s._screenshot_counter += 1
    suffix = "_fullpage" if full_page else ""
    safe_name = f"{s._screenshot_counter:03d}_{name}{suffix}"
    path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")
    await s.browser.screenshot(path, full_page=full_page)
    s.screenshots.append(Screenshot(
        path=path, name=safe_name, step=last_step,
        url=s.browser._page.url if s.browser._page else "",
    ))
    return f"Screenshot saved: {path}"


@mcp.tool()
async def screenshot_diff(
    before: str,
    after: str,
    name: str = "diff",
    threshold: int = 25,
) -> str:
    """Compare two screenshots and produce a third image with changed
    regions highlighted in red, so you can see what visually changed
    between two states.

    Useful for detecting layout shifts, content updates that should not
    have happened, focus-ring changes after a click, modal overlays
    appearing, theme switches, etc. Argus does not auto-judge whether
    a diff is a bug — you read the side-by-side and decide.

    Args:
        before: Path or filename of the earlier screenshot (returned
                from a previous `screenshot()` call).
        after: Path of the later screenshot.
        name: Label for the output diff image.
        threshold: 0-255 per-channel pixel difference above which a
                   pixel is considered "changed". Default 25 (mild).
                   Lower = more sensitive.
    """
    from PIL import Image, ImageChops, ImageDraw

    s = _require_session()
    out_dir = Path(_output_dir()) / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(p: str) -> Optional[Path]:
        candidates = [Path(p)]
        if not Path(p).is_absolute():
            candidates.append(out_dir / Path(p).name)
            candidates.append(Path("argus-reports/screenshots") / Path(p).name)
        for c in candidates:
            if c.exists():
                return c
        return None

    before_path = _resolve(before)
    after_path = _resolve(after)
    if before_path is None:
        return f"screenshot_diff: cannot find {before!r}"
    if after_path is None:
        return f"screenshot_diff: cannot find {after!r}"

    try:
        img_a = Image.open(before_path).convert("RGB")
        img_b = Image.open(after_path).convert("RGB")
    except Exception as exc:
        return f"screenshot_diff: failed to open images: {exc}"

    if img_a.size != img_b.size:
        # Resize the smaller to match — keeps comparison meaningful even
        # when viewport changed slightly.
        target = (
            min(img_a.size[0], img_b.size[0]),
            min(img_a.size[1], img_b.size[1]),
        )
        img_a = img_a.resize(target)
        img_b = img_b.resize(target)

    diff_img = ImageChops.difference(img_a, img_b)
    bbox = diff_img.getbbox()

    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    out_path = out_dir / f"{safe_name}.png"

    if bbox is None:
        # Pixel-identical. Save before image as the diff so the agent has
        # a concrete artifact, but signal that there's no change.
        img_a.save(out_path)
        return (
            f"screenshot_diff: images are pixel-identical — no visible change.\n"
            f"  Saved (copy of before): {out_path}"
        )

    # Build a binary mask of changed pixels at the requested threshold.
    grey = diff_img.convert("L")
    mask = grey.point(lambda v: 255 if v > threshold else 0, mode="L")

    # Composite a translucent red overlay onto the after image at the mask.
    red_layer = Image.new("RGB", img_b.size, (255, 0, 0))
    composite = Image.composite(red_layer, img_b, mask).convert("RGB")
    # Blend back so changed regions are tinted, not solid red.
    blended = Image.blend(img_b, composite, 0.5)

    # Outline the overall bounding box for orientation.
    draw = ImageDraw.Draw(blended)
    draw.rectangle(bbox, outline=(255, 0, 0), width=3)

    blended.save(out_path)

    # Heuristic: how much of the image changed?
    changed_pixels = sum(1 for px in mask.getdata() if px > 0)
    total_pixels = mask.size[0] * mask.size[1]
    pct = (changed_pixels / total_pixels) * 100 if total_pixels else 0

    s.screenshots.append(Screenshot(
        path=str(out_path), name=safe_name, step=f"diff: {before} vs {after}",
        url=s.browser._page.url if s.browser._page else "",
    ))

    return (
        f"screenshot_diff saved: {out_path}\n"
        f"  Changed bounding box: {bbox} (size {bbox[2] - bbox[0]} x {bbox[3] - bbox[1]})\n"
        f"  Approx {pct:.1f}% of pixels changed (threshold={threshold}).\n"
        f"  Decide: is this change expected for the action you took? If a UI region "
        f"changed unexpectedly, that may be a bug — call record_bug."
    )


@mcp.tool()
async def get_errors() -> str:
    """Drain captured browser events (console errors, HTTP 4xx/5xx) since
    the last call.

    These two channels are not visible to you through page state, so Argus
    captures them as Bug records automatically. Use this after any action
    that might have triggered a JS error or backend failure.

    For everything else (visual issues, copy problems, missed validation,
    cross-page inconsistency), read the page state yourself and call
    record_bug when you've confirmed something is a real bug.
    """
    s = _require_session()
    console_errs, network_errs = s.browser.drain_errors()

    current_url = s.browser._page.url if s.browser._page else ""
    new_bugs = s.detector.process_console_errors(
        console_errs, current_url, s.steps
    )
    new_bugs.extend(s.detector.process_network_errors(
        network_errs, current_url, s.steps
    ))

    if new_bugs:
        ss_path = await _auto_screenshot(
            s, f"error_{len(s.bugs) + 1}", f"Error detected on {current_url}"
        )
        for bug in new_bugs:
            bug.screenshot_path = ss_path

    s.bugs.extend(new_bugs)

    if not new_bugs and not console_errs and not network_errs:
        return f"No new console or network events. Total bugs in session: {len(s.bugs)}"

    lines = []
    for err in console_errs:
        lines.append(f"[CONSOLE {err['type'].upper()}] {err['text']}")
    for err in network_errs:
        lines.append(f"[HTTP {err['status']}] {err['method']} {err['url']}")
    if new_bugs:
        lines.append(f"\nCaptured {len(new_bugs)} new event-bug(s).")
    lines.append(f"Total bugs in session: {len(s.bugs)}")

    return "\n".join(lines)


_SEVERITY_BY_NAME = {s.value: s for s in Severity}
_BUG_TYPE_BY_NAME = {t.value: t for t in BugType}


@mcp.tool()
async def record_bug(
    title: str,
    severity: str,
    evidence: Optional[dict] = None,
) -> str:
    """Record a confirmed bug you have identified during testing.

    Call this only after you have observed something that meets the bug
    bar: reproducible, user-affecting, persistent. Do not record
    speculation or polish nits. The session report is built from these
    records — be specific.

    Args:
        title: One-line headline, specific. Bad: "Form has issues."
               Good: "Login form accepts any password — no authentication."
        severity: "critical" | "high" | "medium" | "low" | "info".
                  HIGH = data loss / security / payment / blocked flow.
                  MEDIUM = workflow friction / confusing UX / cross-page bug.
                  LOW = polish / suggestion-grade.
        evidence: Optional dict with extra context. Recommended keys:
            description (str): Longer explanation including user impact.
                Default = same as title.
            steps (list[str]): Reproduction steps. Default = current
                session step log (everything you did so far).
            url (str): Page or screen URL. Default = current page URL.
            screenshot (str): One of "auto" (default — take one now and
                attach), "skip" (no screenshot), or a label to use as
                the screenshot filename. Pre-existing screenshot paths
                are also accepted.
            bug_type (str): A category for the report. Default
                "ux_issue". One of: console_error, network_error,
                visual_anomaly, ux_issue, crash, broken_link,
                form_error, state_verification, misleading_success,
                count_mismatch, text_anomaly, broken_image, seo_issue,
                accessibility, performance, mixed_content.
    """
    s = _require_session()

    sev_key = (severity or "").strip().lower()
    if sev_key not in _SEVERITY_BY_NAME:
        return (
            f"record_bug: invalid severity {severity!r}. "
            f"Use one of: {', '.join(_SEVERITY_BY_NAME)}."
        )
    sev = _SEVERITY_BY_NAME[sev_key]

    ev = evidence or {}
    description = ev.get("description") or title
    steps = ev.get("steps") or list(s.steps)
    if ev.get("url"):
        url = ev["url"]
    elif s.mode == "web" and s.browser is not None and s.browser._page is not None:
        url = s.browser._page.url
    elif s.mode == "screen" and s.screen is not None and s.screen._app_name:
        url = f"screen://{s.screen._app_name}"
    else:
        url = ""

    type_key = (ev.get("bug_type") or "ux_issue").strip().lower()
    if type_key not in _BUG_TYPE_BY_NAME:
        return (
            f"record_bug: invalid bug_type {type_key!r}. "
            f"Use one of: {', '.join(_BUG_TYPE_BY_NAME)}."
        )
    bug_type = _BUG_TYPE_BY_NAME[type_key]

    screenshot_directive = ev.get("screenshot", "auto")
    screenshot_path: Optional[str] = None
    if screenshot_directive == "skip":
        pass
    elif screenshot_directive in ("auto", "", None):
        label = "bug_" + "".join(c if c.isalnum() else "_" for c in title.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {title[:60]}")
    elif "/" in screenshot_directive or screenshot_directive.endswith(".png"):
        # Treat as an existing path
        screenshot_path = screenshot_directive
    else:
        # Treat as a label for a fresh screenshot
        label = "".join(c if c.isalnum() else "_" for c in screenshot_directive.lower())[:40]
        screenshot_path = await _auto_screenshot(s, label, f"record_bug: {title[:60]}")

    bug = Bug(
        type=bug_type,
        severity=sev,
        title=title,
        description=description,
        url=url,
        steps_to_reproduce=list(steps),
        screenshot_path=screenshot_path,
    )
    s.bugs.append(bug)
    s.steps.append(f"record_bug: [{sev.value}] {title}")

    out = [
        f"Recorded bug [{sev.value.upper()}] {title}",
        f"  url: {url}",
        f"  type: {bug_type.value}",
        f"  steps: {len(steps)} step(s)",
    ]
    if screenshot_path:
        out.append(f"  screenshot: {screenshot_path}")
    out.append(f"  total bugs in session: {len(s.bugs)}")
    return "\n".join(out)


@mcp.tool()
async def verify_persistence(
    expect: str,
    target_text: str,
    after_url: str = "",
) -> str:
    """Force a fresh page load and report whether `target_text` is present
    or absent — your tool for catching the "Saved!" toast that lied.

    After any destructive or persistence-changing action (delete, edit,
    save, submit, toggle, payment, etc.), the success toast is not
    proof. Only a fresh GET on the relevant page is. This tool does
    that GET and reports presence — you decide whether the result
    matches what you expected.

    Examples:
      verify_persistence("absent", "Buy groceries", "/tasks")
        — after deleting "Buy groceries", confirm it's gone from the list.
      verify_persistence("present", "EDITED-VALUE-XYZ", "/tasks/1/edit")
        — after editing, confirm the new value reloads.

    Argus does not auto-record a bug here. If presence does not match
    your expectation, call record_bug.

    Args:
        expect: "present" or "absent" — what state the target_text
                should be in after the fresh page load.
        target_text: The text or value you're checking for.
        after_url: Page to load and inspect. Defaults to the current URL.
    """
    s = _require_session()
    expect_norm = (expect or "").strip().lower()
    if expect_norm not in ("present", "absent"):
        return (
            f"verify_persistence: invalid `expect` {expect!r}. "
            f"Use 'present' or 'absent'."
        )

    s.steps.append(f'verify_persistence({expect_norm}, {target_text[:60]!r})')

    current_url = s.browser._page.url if s.browser._page else ""
    nav_url = after_url or current_url
    await s.browser.goto(nav_url)
    after_state = await s.browser.get_state()
    s._last_elements = after_state.elements

    present = _text_in_state(target_text, after_state)
    matches = (present and expect_norm == "present") or (
        not present and expect_norm == "absent"
    )

    if expect_norm == "absent":
        verdict = (
            "Target text is GONE after refresh — matches expectation."
            if not present
            else "Target text is STILL PRESENT after refresh — does NOT match expectation."
        )
    else:
        verdict = (
            "Target text is PRESENT after refresh — matches expectation."
            if present
            else "Target text is MISSING after refresh — does NOT match expectation."
        )

    return (
        f"verify_persistence(expect={expect_norm!r}, target={target_text[:60]!r}) "
        f"on {nav_url}\n"
        f"  Result: {'MATCH' if matches else 'MISMATCH'} — {verdict}\n"
        f"  Decide: if this mismatch is a real bug (silent delete failure, "
        f"edit not persisting, etc.), call record_bug. Argus does not infer "
        f"that for you."
    )


@mcp.tool()
async def check_links() -> str:
    """Probe every internal link on the current page (HEAD with GET fallback)
    and return raw status-code results.

    Argus does not auto-record bugs here. You read the dead-link list and
    decide. A handful of dead anchor links is rarely shippable; the
    severity depends on context. Call record_bug for the ones that matter.
    """
    s = _require_session()
    state = await s.browser.get_state()
    s._last_elements = state.elements

    link_results = await s.browser.check_links(state.links)
    dead = [r for r in link_results if not r["ok"]]
    alive = [r for r in link_results if r["ok"]]
    external = [l for l in state.links if not l.get("isInternal")]

    lines = [f"Checked {len(link_results)} internal links on {state.url}"]
    lines.append(f"  OK: {len(alive)}")
    lines.append(f"  Dead: {len(dead)}")
    lines.append(f"  External (not checked): {len(external)}")
    if dead:
        lines.append("")
        for r in dead:
            lines.append(f"  [HTTP {r['status']}] {r['href']}")
        lines.append("")
        lines.append("Decide: are any of these real bugs? Call record_bug if so.")
    return "\n".join(lines)


@mcp.tool()
async def check_performance() -> str:
    """Read raw performance metrics from the browser's Performance API
    (load time, TTFB, request count, large resources).

    Argus does not auto-record bugs here — Lighthouse already owns the
    performance-audit space. Only call record_bug if the page is so slow
    or so heavy that it materially blocks a real user (multi-second TTFB
    on a primary flow, multi-MB hero asset, etc).
    """
    s = _require_session()
    current_url = s.browser._page.url if s.browser._page else ""
    perf_data = await s.browser.get_performance()

    nav = perf_data.get("navigation", {})
    summary = perf_data.get("summary", {})
    lines = [f"Performance for {current_url}"]
    if nav:
        lines.append(f"  Load time: {nav.get('loadTime', 0)/1000:.2f}s")
        lines.append(f"  TTFB: {nav.get('ttfb', 0)/1000:.2f}s")
        lines.append(f"  DOM interactive: {nav.get('domInteractive', 0)/1000:.2f}s")
    lines.append(f"  Total requests: {summary.get('totalRequests', '?')}")
    lines.append(f"  Total size: {summary.get('totalSize', 0)/1024:.0f} KB")
    large = perf_data.get("resources", [])
    if large:
        lines.append(f"  Large resources (>500KB):")
        for r in large:
            lines.append(f"    {r['size']/1024:.0f}KB — {r['name'][:80]}")
    return "\n".join(lines)


@mcp.tool()
async def crawl_site(max_pages: int = 20) -> str:
    """Crawl the entire site starting from the current page. Visits all internal links,
    runs all detectors on each page, and checks links and performance.

    This is the most thorough scan — it discovers pages automatically and tests everything.
    Can take 30-120 seconds depending on site size.

    Args:
        max_pages: Maximum number of pages to visit (default 20)
    """
    from urllib.parse import urlparse, urlunparse

    s = _require_session()
    start_url = s.browser._page.url if s.browser._page else s.url
    visited: set = set()
    visited_paths: set = set()  # track by path to avoid ?ref=x duplicates
    to_visit: list = [start_url]
    page_results: list = []

    def _normalize(u: str) -> str:
        """Strip query params and fragments for dedup."""
        p = urlparse(u)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        normalized = _normalize(url)
        if normalized in visited_paths:
            continue
        visited.add(url)
        visited_paths.add(normalized)

        try:
            await s.browser.goto(url)
        except Exception:
            continue

        state = await s.browser.get_state()
        s._last_elements = state.elements
        if state.url not in s.pages_visited:
            s.pages_visited.append(state.url)

        # Capture only what the agent cannot see directly: console + network events.
        console_errs, network_errs = s.browser.drain_errors()
        new_bugs = s.detector.process_console_errors(console_errs, state.url, s.steps)
        new_bugs.extend(s.detector.process_network_errors(network_errs, state.url, s.steps))

        # Probe links once (raw probe, no auto-bug — agent decides).
        link_results = await s.browser.check_links(state.links)
        dead = [r for r in link_results if not r["ok"]]

        # Screenshot when console/network captured something.
        if new_bugs:
            page_name = state.url.split("/")[-1] or "index"
            ss_path = await _auto_screenshot(s, f"crawl_{page_name}", f"Crawl: {state.url}")
            for bug in new_bugs:
                bug.screenshot_path = ss_path

        s.bugs.extend(new_bugs)
        page_results.append((state.url, len(new_bugs), len(dead)))

        # Discover new internal links to visit (deduplicated by path)
        for link in state.links:
            href = link.get("href", "")
            if link.get("isInternal") and _normalize(href) not in visited_paths and href not in to_visit:
                to_visit.append(href)

    lines = [f"Crawl complete: {len(visited)} pages visited, {len(s.bugs)} event-bugs captured"]
    lines.append("")
    for url, bug_count, dead_count in page_results:
        markers = []
        if bug_count:
            markers.append(f"{bug_count} event-bug(s)")
        if dead_count:
            markers.append(f"{dead_count} dead link(s)")
        marker = f"  ({', '.join(markers)})" if markers else ""
        lines.append(f"  {url}{marker}")
    lines.append("")
    lines.append("Crawl auto-records only console/network events. For everything")
    lines.append("else, observe the pages yourself and call record_bug.")
    return "\n".join(lines)


# ── Compound Action Tools ─────────────────────────────────────────


@mcp.tool()
async def test_action(target: str, expectation: str = "") -> str:
    """Click an element and observe the diff in one round-trip.

    Captures the state before and after a click on the element matching
    `target`, computes a structural diff, drains console/network events,
    and screenshots the result. This is a convenience wrapper around
    observe + click_what + observe — useful when you want a single
    tool call to learn what changed.

    Argus does not auto-judge whether `expectation` was met. You read
    the diff and decide; if you observed a real bug, call record_bug.

    Args:
        target: Natural-language description of the element to click,
                same syntax as click_what ("Login button", "Add Task",
                "Delete near Buy groceries").
        expectation: Optional one-line note on what you expected to
                happen — used as the action description in the diff
                output. Purely informational.
    """
    s = _require_session()

    el, err = _resolve_or_error(s, target)
    if err:
        return err

    label = el.text or el.aria_label or el.placeholder or el.name or el.tag
    step = f'test_action({target!r}) -> "{label[:60]}"'
    if expectation:
        step += f" — expected: {expectation}"
    s.steps.append(step)

    s.browser.drain_errors()
    before = await s.browser.get_state()

    selector = s.browser._build_selector(el)
    try:
        await s.browser._page.click(selector, timeout=5000)
        await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as exc:
        ss = await _auto_screenshot(s, "action_failed", step)
        return (
            f"test_action({target!r}) — click failed: {exc}\n"
            f"The element may be obscured, stale, or removed. "
            f"Screenshot: {ss}"
        )

    await asyncio.sleep(0.3)
    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _capture_browser_events(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, expectation or target)

    ss_path = await _auto_screenshot(s, f"action_{label[:20]}", step)
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

    lines = [
        f'ACTION: Clicked "{label[:60]}" via target {target!r}',
    ]
    if expectation:
        lines.append(f"  Expected: {expectation}")
    lines.append("")
    lines.append("CHANGES:")
    for c in changes:
        lines.append(f"  {c}")
    lines.append("")

    if console_errs or network_errs:
        lines.append("BROWSER EVENTS:")
        for err in console_errs[:3]:
            lines.append(f"  [CONSOLE] {err['text'][:80]}")
        for err in network_errs[:3]:
            lines.append(f"  [HTTP {err['status']}] {err['method']} {err['url'][:60]}")
    else:
        lines.append("BROWSER EVENTS: none")

    if new_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(new_bugs)} new):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")
    lines.append(
        "\nDecide: did anything you observed warrant a record_bug call? "
        "test_action does not infer that for you."
    )
    lines.append(f"Screenshot: {ss_path}")
    return "\n".join(lines)


@mcp.tool()
async def test_form(
    form_fields: dict,
    submit: str = "auto",
) -> str:
    """Fill a set of form fields and submit, in one call. Description-keyed.

    Each key in `form_fields` is matched to an input via the same
    natural-language resolver used by type_into / select_into. Use
    descriptions that match the field's label, name, or placeholder:
    {"email": "alice@x.com", "password": "abc12345", "confirm": "abc12345"}.

    Argus reports what happened (URL change, new feedback messages,
    captured browser events, structural diff). It does NOT label the
    outcome as success / failure — your job is to read the result and
    call record_bug if you've confirmed a real bug.

    Args:
        form_fields: Dict {field_description: value}.
        submit: How to submit. "auto" (default) finds the most likely
            submit button by text. "enter" presses Enter on the last
            filled field. Otherwise, treated as a description for
            click_what to resolve (e.g. "Save Changes" or "Register").
    """
    s = _require_session()

    s.browser.drain_errors()
    before = await s.browser.get_state()
    before_url = before.url
    s._last_elements = before.elements

    field_results = []
    for field_desc, value in form_fields.items():
        # Re-observe before each field; SPA re-renders shift element refs.
        state = await s.browser.get_state()
        s._last_elements = state.elements

        result = resolve_element(field_desc, state.elements, kind_filter="input")
        if result.reason != "unique" or result.found is None:
            # Try without the kind filter — sometimes selects or buttons are intended
            result_any = resolve_element(field_desc, state.elements)
            if result_any.reason != "unique" or result_any.found is None:
                field_results.append(f'[MISS] {field_desc!r} — {result.reason}')
                continue
            el = result_any.found
        else:
            el = result.found

        selector = s.browser._build_selector(el)
        try:
            if el.tag == "select":
                await s.browser._page.select_option(selector, str(value), timeout=5000)
            else:
                await s.browser._page.fill(selector, str(value), timeout=5000)
            field_results.append(f'[OK] {field_desc!r} -> {value!r}')
            s.steps.append(f'Type {value!r} into {field_desc!r}')
        except Exception as exc:
            field_results.append(f'[FAIL] {field_desc!r} — {exc}')

    # Submit
    state = await s.browser.get_state()
    s._last_elements = state.elements
    submit_label = ""

    if submit == "enter":
        # Press Enter on whatever element is currently focused, falling back
        # to the last filled field if we can determine it.
        try:
            await s.browser._page.keyboard.press("Enter")
            await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
            submit_label = "Enter key"
            s.steps.append("Press Enter to submit")
        except Exception as exc:
            ss = await _auto_screenshot(s, "form_no_submit", "Enter-key submit failed")
            return f"FORM: filled {len(field_results)} fields but Enter-submit failed: {exc}"
    else:
        if submit == "auto":
            # Auto-discover: prefer a button whose text matches typical submit verbs.
            submit_keywords = [
                "submit", "save", "create", "sign in", "log in", "login",
                "register", "send", "add", "continue",
            ]
            scored: list[tuple[int, InteractiveElement]] = []
            for el in state.elements:
                if el.tag not in ("button", "input"):
                    continue
                if el.tag == "input" and el.type not in ("submit", "button"):
                    continue
                text = (el.text or "").lower()
                score = 0
                for kw in submit_keywords:
                    if kw in text:
                        score = max(score, 50 + len(kw))
                if el.type == "submit":
                    score = max(score, 80)
                if score > 0:
                    scored.append((score, el))
            scored.sort(key=lambda p: -p[0])
            target_el = scored[0][1] if scored else None
        else:
            result = resolve_element(submit, state.elements, kind_filter="button")
            if result.reason != "unique" or result.found is None:
                return (
                    f"FORM: filled {len(field_results)} fields but submit "
                    f"description {submit!r} did not resolve: {result.reason}.\n"
                    + "\n".join(field_results)
                )
            target_el = result.found

        if target_el is None:
            ss = await _auto_screenshot(s, "form_no_submit", "Could not find submit button")
            return (
                f"FORM: filled {len(field_results)} fields but no submit button "
                f"found. Pass submit=\"enter\" or submit=\"<description>\" "
                f"explicitly.\n" + "\n".join(field_results)
            )

        submit_label = target_el.text or target_el.aria_label or "submit"
        s.steps.append(f'Click {submit_label!r}')
        sel = s.browser._build_selector(target_el)
        try:
            await s.browser._page.click(sel, timeout=5000)
            await s.browser._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as exc:
            ss = await _auto_screenshot(s, "form_submit_fail", "Submit click failed")
            return (
                f"FORM: filled {len(field_results)} fields but submit click failed: {exc}\n"
                + "\n".join(field_results)
            )

    await asyncio.sleep(0.3)

    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _capture_browser_events(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, "form submission")

    redirected = after.url != before_url
    ss_path = await _auto_screenshot(s, "form_result", f"Form: {submit_label}")
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

    lines = [
        f'FORM SUBMISSION via "{submit_label}"',
        "",
        "FIELDS:",
    ]
    for fr in field_results:
        lines.append(f"  {fr}")
    lines.append("")
    lines.append("RESULT:")
    lines.append(
        f"  URL: {before_url} -> {after.url} "
        f"{'(redirected)' if redirected else '(same page)'}"
    )
    if after.toast_messages:
        lines.append(f"  Feedback: {', '.join(after.toast_messages[:3])}")
    lines.append("")
    lines.append("CHANGES:")
    for c in changes:
        lines.append(f"  {c}")

    if console_errs or network_errs:
        lines.append("")
        lines.append("BROWSER EVENTS:")
        for err in console_errs[:3]:
            lines.append(f"  [CONSOLE] {err['text'][:80]}")
        for err in network_errs[:3]:
            lines.append(f"  [HTTP {err['status']}] {err['method']} {err['url'][:60]}")

    if new_bugs:
        lines.append(f"\nEvent-bugs auto-captured ({len(new_bugs)} new):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")

    lines.append(
        "\nDecide: was the outcome what you expected? If the form accepted "
        "garbage / lost data / showed a misleading toast / etc., call record_bug."
    )
    lines.append(f"Screenshot: {ss_path}")
    return "\n".join(lines)



@mcp.tool()
async def end_session() -> str:
    """End the testing session, close the browser, and generate an HTML error report.

    Returns the path to the generated report and a summary of findings.
    """
    s = _require_session()

    if s.mode == "web" and s.browser is not None:
        # Final error drain (web mode only — console/network are web concepts).
        console_errs, network_errs = s.browser.drain_errors()
        current_url = s.browser._page.url if s.browser._page else ""
        final_bugs = s.detector.process_console_errors(
            console_errs, current_url, s.steps
        )
        final_bugs.extend(s.detector.process_network_errors(
            network_errs, current_url, s.steps
        ))
        if final_bugs:
            ss_path = await _auto_screenshot(
                s, "final_errors", f"Final errors on {current_url}"
            )
            for bug in final_bugs:
                bug.screenshot_path = ss_path
        s.bugs.extend(final_bugs)
        await s.browser.stop()
    elif s.mode == "screen" and s.screen is not None:
        await s.screen.stop()

    duration = asyncio.get_event_loop().time() - (s.start_time or 0)

    target = s.url if s.mode == "web" else f"screen://{s.mode}"
    result = ExplorationResult(
        url=target or "",
        bugs=s.bugs,
        pages_visited=s.pages_visited,
        actions_taken=len(s.steps),
        duration_seconds=duration,
        focus_areas=s.focus_areas,
        screenshots=s.screenshots,
    )

    output_dir = _output_dir()
    reporter = Reporter()
    report_path = reporter.generate(result, output_dir)

    # Reset session
    global _session
    _session = Session()

    # Build summary
    lines = [
        f"Session ended. Report saved: {report_path}",
        f"",
        f"Summary:",
        f"  Actions taken: {result.actions_taken}",
        f"  Pages visited: {len(result.pages_visited)}",
        f"  Bugs found: {len(result.bugs)}",
        f"  Screenshots: {len(result.screenshots)}",
        f"  Duration: {duration:.1f}s",
    ]
    if result.bugs:
        lines.append("")
        lines.append("Bugs:")
        for bug in result.bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title}")

    return "\n".join(lines)


def main():
    """Entry point for argus-mcp command.

    Flags:
      --unsafe       Enable eval_js (off by default; can read cookies,
                     mutate state, and fetch arbitrary URLs from the
                     page context).
      --doctor       Run the macOS screen-mode permission check and
                     exit. Use this before launching screen mode for
                     the first time.

    Without flags, just runs the MCP server over stdio.
    """
    import sys as _sys

    if "--doctor" in _sys.argv:
        from .screen.permissions import main as _doctor
        _sys.exit(_doctor())

    if "--unsafe" in _sys.argv:
        os.environ["ARGUS_UNSAFE_EVAL"] = "1"
        _sys.argv = [a for a in _sys.argv if a != "--unsafe"]

    mcp.run()


if __name__ == "__main__":
    main()
