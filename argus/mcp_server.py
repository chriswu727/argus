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
from .models import Bug, BugType, ExplorationResult, PageState, Screenshot, Severity
from .reporter import Reporter

mcp = FastMCP(
    "argus",
    instructions="""Argus is an AI-powered QA testing toolkit. Use these tools to explore a web application like a real user. Typical workflow:
1. start_session(url) — launch browser
2. get_page_state() — see interactive elements
3. click/type/navigate — interact with the page
4. get_errors() — check for console/network errors
5. screenshot() — capture the current state
6. Repeat 2-5, exploring systematically
7. end_session() — close browser and generate report

TESTING STRATEGY — Be thorough and systematic:

PAGE DISCOVERY: Visit every link in the navigation. Check for dead links (404s).

FORM TESTING — For every form you find, test ALL of these:
  - Empty submission: submit with no fields filled
  - Valid data: fill everything correctly and submit
  - Invalid emails: try "notanemail", "a@", "@b.com"
  - Password edge cases: try very short (1 char), very long (200+ chars), empty
  - Special characters: < > " ' & ; | \\ / {{ }} and unicode like 中文 émojis 🔥
  - SQL injection: try ' OR 1=1 -- and "; DROP TABLE users; --
  - XSS payloads: try <script>alert('xss')</script> and <img onerror=alert(1) src=x>
  - Very long input: paste 500+ characters into fields
  - Mismatched fields: if there's "confirm password", make them different
  - Boundary values: 0, -1, 999999999 for numeric fields
  - Required field bypass: fill some fields but skip required ones

NAVIGATION TESTING:
  - Click every navigation link
  - Try browser back/forward
  - Try navigating to pages directly by URL
  - Check what happens when accessing authenticated pages without login

INTERACTION TESTING:
  - Click every button, even ones that look decorative
  - Try double-clicking
  - Try actions in unexpected order (e.g., submit before filling form)
  - Try the same action multiple times rapidly

Always call get_errors() after form submissions and page navigations.
Always call screenshot() after important interactions to document findings.
Take a screenshot of EVERY page you visit and every error state you find.

VERIFICATION — After destructive actions, ALWAYS verify:
  - After DELETE: call verify_action("delete", "item text", verify_url="/list-page") to confirm item is gone
  - After EDIT/SAVE: call verify_action("edit", "new value", verify_url="/edit-page") to confirm changes persisted
  - The verify_url should be the page where you can SEE the data (e.g., the list page for delete, the edit form for edit)
  - After seeing a success toast: call get_errors() immediately to check for hidden server errors

CONTENT ANALYSIS — get_page_state() now returns:
  - Full page text (check for "Loading...", "NaN", broken dates)
  - Visible toast messages
  - Displayed counts (compare against actual item counts)
  - CSS state indicators

Look for these patterns:
  - "Loading..." text on a page that should have loaded
  - Numbers with decimals in dates ("1.52 days ago")
  - Count mismatches between totals and actual items
  - Success messages alongside server errors
  - Red/alarming styling on zero-remaining states

COMPREHENSIVE CHECKS — These run automatically with get_errors():
  - Broken images (failed to load)
  - Missing alt text on images
  - Form inputs without labels
  - Buttons/links with no accessible name
  - Missing meta description, OG tags
  - Heading hierarchy issues
  - Mixed content (HTTP on HTTPS)

COMPOUND TOOLS (preferred — do more per call):
  - test_action(index, description): Click + auto-verify. Captures before/after state, diffs, detects bugs.
  - test_form(fields, submit_text): Fill form + submit + verify result. One call replaces 5+ low-level calls.
  - test_crud(create_url, list_url, item_data): Full create/edit/delete cycle with auto-verification.

ON-DEMAND CHECKS:
  - check_links(): Crawl all internal links, find 404s/5xx
  - check_performance(): Measure load time, find large resources
  - crawl_site(): Auto-visit all pages and run all detectors""",
)


class Session:
    """Holds the state for one testing session."""

    def __init__(self):
        self.browser: Optional[BrowserDriver] = None
        self.detector = Detector()
        self.bugs: List[Bug] = []
        self.steps: List[str] = []
        self.pages_visited: List[str] = []
        self.screenshots: List[Screenshot] = []
        self.start_time: Optional[float] = None
        self.url: Optional[str] = None
        self.focus_areas: List[str] = []
        self._last_elements = []
        self._screenshot_counter = 0

    @property
    def active(self) -> bool:
        return self.browser is not None


_session = Session()


def _require_session() -> Session:
    if not _session.active:
        raise RuntimeError("No active session. Call start_session(url) first.")
    return _session


def _output_dir() -> str:
    return os.environ.get("ARGUS_OUTPUT_DIR", "./argus-reports")


async def _auto_screenshot(s: Session, name: str, step: str) -> str:
    """Take a screenshot and register it in the session."""
    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    path = str(Path(_output_dir()) / "screenshots" / f"{safe_name}.png")
    await s.browser.screenshot(path)
    url = s.browser._page.url if s.browser._page else ""
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


async def _run_all_detectors(
    s: Session, state: PageState, console_errs: list, network_errs: list
) -> list:
    """Run all passive detectors and return new bugs."""
    new_bugs = s.detector.process_console_errors(console_errs, state.url, s.steps)
    new_bugs.extend(s.detector.process_network_errors(network_errs, state.url, s.steps))
    new_bugs.extend(s.detector.process_page_content(state, s.steps))
    new_bugs.extend(s.detector.process_count_consistency(state, s.steps))
    new_bugs.extend(s.detector.process_css_indicators(state, s.steps))
    new_bugs.extend(s.detector.process_broken_images(state, s.steps))
    new_bugs.extend(s.detector.process_accessibility(state, s.steps))
    new_bugs.extend(s.detector.process_seo(state, s.steps))
    new_bugs.extend(s.detector.process_mixed_content(state, s.steps))
    if state.toast_messages:
        new_bugs.extend(s.detector.process_toast_network_crosscheck(
            state.toast_messages, network_errs, state.url, s.steps
        ))
    return new_bugs


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
        f"Session started.\n"
        f"Page: {state.title}\n"
        f"URL: {state.url}\n"
        f"Found {element_count} interactive elements. "
        f"Call get_page_state() to see them."
    )


@mcp.tool()
async def get_page_state() -> str:
    """Get the current page URL, title, and all interactive elements.

    Returns a numbered list of elements you can interact with using
    click(index), type_text(index, text), or select_option(index, value).
    """
    s = _require_session()
    state = await s.browser.get_state()
    s._last_elements = state.elements

    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)

    lines = [f"URL: {state.url}", f"Title: {state.title}", "", "Interactive elements:"]
    if not state.elements:
        lines.append("  (none found)")
    for el in state.elements:
        parts = [f"  [{el.index}] <{el.tag}"]
        if el.type:
            parts.append(f' type="{el.type}"')
        parts.append(">")
        if el.text:
            parts.append(f' "{el.text}"')
        if el.placeholder:
            parts.append(f' (placeholder: "{el.placeholder}")')
        if el.href:
            parts.append(f" -> {el.href}")
        if el.disabled:
            parts.append(" [disabled]")
        if el.value:
            parts.append(f' value="{el.value}"')
        lines.append("".join(parts))

    # Form/action hints for AI
    form_inputs = [e for e in state.elements if e.tag in ("input", "textarea", "select") and e.type not in ("hidden", "submit", "button")]
    if form_inputs:
        lines.append("")
        lines.append(f"Forms detected: {len(form_inputs)} input fields")
        lines.append("  TIP: Use test_form() to fill and submit this form with auto-verification")

    # Page content analysis
    if state.page_text:
        lines.append("")
        lines.append(f"Page text (first 1500 chars):")
        lines.append(state.page_text[:1500])
    if state.toast_messages:
        lines.append("")
        lines.append("Visible toasts/notifications:")
        for toast in state.toast_messages:
            lines.append(f"  [TOAST] {toast}")
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

    return "\n".join(lines)


@mcp.tool()
async def click(element_index: int) -> str:
    """Click an interactive element by its index number from get_page_state().

    Args:
        element_index: The [N] index of the element to click
    """
    s = _require_session()
    if element_index < 0 or element_index >= len(s._last_elements):
        return f"Error: element index {element_index} out of range (0-{len(s._last_elements) - 1})"

    el = s._last_elements[element_index]
    label = el.text or el.aria_label or el.placeholder or f"{el.tag}#{el.id or '?'}"
    step = f'Click "{label}"'
    s.steps.append(step)

    ok = await s.browser.click(element_index, s._last_elements)
    if ok:
        new_state = await s.browser.get_state()
        s._last_elements = new_state.elements
        if new_state.url not in s.pages_visited:
            s.pages_visited.append(new_state.url)
        return f"Clicked \"{label}\". Now on: {new_state.url} ({len(new_state.elements)} elements)"
    return f"Failed to click \"{label}\" — element may be obscured or gone."


@mcp.tool()
async def type_text(element_index: int, text: str) -> str:
    """Type text into an input element.

    Args:
        element_index: The [N] index of the input element
        text: The text to type
    """
    s = _require_session()
    if element_index < 0 or element_index >= len(s._last_elements):
        return f"Error: element index {element_index} out of range"

    el = s._last_elements[element_index]
    label = el.placeholder or el.name or el.id or f"element [{element_index}]"
    step = f'Type "{text}" into {label}'
    s.steps.append(step)

    ok = await s.browser.type_text(element_index, text, s._last_elements)
    if ok:
        return f'Typed "{text}" into {label}'
    return f"Failed to type into {label}"


@mcp.tool()
async def select_option(element_index: int, value: str) -> str:
    """Select an option from a dropdown/select element.

    Args:
        element_index: The [N] index of the select element
        value: The value or visible text to select
    """
    s = _require_session()
    if element_index < 0 or element_index >= len(s._last_elements):
        return f"Error: element index {element_index} out of range"

    step = f'Select "{value}" in element [{element_index}]'
    s.steps.append(step)

    ok = await s.browser.select_option(element_index, value, s._last_elements)
    if ok:
        return f'Selected "{value}"'
    return "Failed to select option"


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
    return "Scrolled down. Call get_page_state() to see updated elements."


@mcp.tool()
async def screenshot(name: str = "screenshot") -> str:
    """Take a screenshot of the current page.

    Args:
        name: Name for the screenshot file (without extension)
    """
    s = _require_session()
    last_step = s.steps[-1] if s.steps else "Initial state"
    path = await _auto_screenshot(s, name, last_step)
    return f"Screenshot saved: {path}"


@mcp.tool()
async def get_errors() -> str:
    """Get all console errors and network failures captured since the last check.

    Returns any JavaScript errors, unhandled exceptions, and HTTP 4xx/5xx
    responses detected during interaction. These are automatically recorded
    as bugs in the session report.
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

    # Smart detection: page content, counts, CSS, toast cross-check
    state = await s.browser.get_state()
    s._last_elements = state.elements
    new_bugs.extend(s.detector.process_page_content(state, s.steps))
    new_bugs.extend(s.detector.process_count_consistency(state, s.steps))
    new_bugs.extend(s.detector.process_css_indicators(state, s.steps))
    if state.toast_messages:
        new_bugs.extend(s.detector.process_toast_network_crosscheck(
            state.toast_messages, network_errs, current_url, s.steps
        ))
    # Comprehensive detection: images, a11y, SEO, mixed content
    new_bugs.extend(s.detector.process_broken_images(state, s.steps))
    new_bugs.extend(s.detector.process_accessibility(state, s.steps))
    new_bugs.extend(s.detector.process_seo(state, s.steps))
    new_bugs.extend(s.detector.process_mixed_content(state, s.steps))

    # Auto-screenshot for new bugs and attach to them
    if new_bugs:
        ss_path = await _auto_screenshot(
            s, f"error_{len(s.bugs) + 1}", f"Error detected on {current_url}"
        )
        for bug in new_bugs:
            bug.screenshot_path = ss_path

    s.bugs.extend(new_bugs)

    if not new_bugs:
        return f"No new errors. Total bugs in session: {len(s.bugs)}"

    lines = []
    for err in console_errs:
        lines.append(f"[CONSOLE {err['type'].upper()}] {err['text']}")
    for err in network_errs:
        lines.append(f"[HTTP {err['status']}] {err['method']} {err['url']}")
    for bug in new_bugs:
        if bug.type not in (BugType.CONSOLE_ERROR, BugType.NETWORK_ERROR):
            lines.append(f"[{bug.type.value.upper()}] {bug.title}")
    lines.append(f"\nTotal bugs in session: {len(s.bugs)}")

    return "\n".join(lines)


@mcp.tool()
async def verify_action(action_type: str, target_text: str, verify_url: str = "") -> str:
    """Verify that a destructive action actually persisted by navigating to a page and checking.

    Call this AFTER performing a delete, edit, or toggle and seeing a success message.
    Navigates to verify_url (or the current page via GET) and checks whether the
    expected change is reflected in the page content.

    Args:
        action_type: One of "delete", "edit", or "toggle"
        target_text: For delete: the text of the deleted item. For edit: the NEW value you entered.
        verify_url: URL to navigate to for verification (e.g. the list page or edit page). If empty, navigates to the current URL via GET.
    """
    s = _require_session()
    s.steps.append(f'Verify {action_type}: "{target_text}"')

    # Navigate to verify URL (GET request, not reload which may re-POST)
    current_url = s.browser._page.url if s.browser._page else ""
    nav_url = verify_url or current_url
    await s.browser.goto(nav_url)
    after_state = await s.browser.get_state()
    s._last_elements = after_state.elements

    # For before_state, we use a minimal PageState since we already navigated away
    from .models import PageState
    before_state = PageState(url=current_url, title="", elements=[], page_text="")

    bugs = s.detector.process_state_verification(
        action_type, target_text, before_state, after_state, s.steps
    )

    if bugs:
        ss_path = await _auto_screenshot(
            s, f"verify_{action_type}", f"Verification failed: {action_type}"
        )
        for bug in bugs:
            bug.screenshot_path = ss_path
        s.bugs.extend(bugs)
        bug_msgs = [f"  [{b.severity.value.upper()}] {b.title}" for b in bugs]
        return "VERIFICATION FAILED:\n" + "\n".join(bug_msgs) + f"\n\nTotal bugs: {len(s.bugs)}"

    return f"Verification passed: {action_type} on '{target_text}' persisted correctly."


@mcp.tool()
async def check_links() -> str:
    """Crawl all internal links on the current page and check for dead links (404/5xx).

    Sends HEAD requests to each internal link found on the page.
    This can take several seconds on pages with many links.
    """
    s = _require_session()
    state = await s.browser.get_state()
    s._last_elements = state.elements

    link_results = await s.browser.check_links(state.links)
    new_bugs = s.detector.process_dead_links(link_results, state.url, s.steps)

    if new_bugs:
        ss_path = await _auto_screenshot(s, "dead_links", f"Dead links on {state.url}")
        for bug in new_bugs:
            bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

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
    lines.append(f"\nTotal bugs in session: {len(s.bugs)}")
    return "\n".join(lines)


@mcp.tool()
async def check_performance() -> str:
    """Measure page performance: load time, resource count, large resources.

    Uses the browser's Performance API. Best called right after navigating to a page.
    """
    s = _require_session()
    current_url = s.browser._page.url if s.browser._page else ""
    perf_data = await s.browser.get_performance()
    new_bugs = s.detector.process_performance(perf_data, current_url, s.steps)

    if new_bugs:
        ss_path = await _auto_screenshot(s, "performance", f"Perf issues on {current_url}")
        for bug in new_bugs:
            bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

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
    lines.append(f"\nTotal bugs in session: {len(s.bugs)}")
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

        # Check if page is blocked (403/error page with no real content)
        console_errs, network_errs = s.browser.drain_errors()
        page_blocked = any(
            e.get("status") in (403, 401) and e.get("url", "").rstrip("/") == url.rstrip("/")
            for e in network_errs
        )

        # Run all passive detectors
        new_bugs = s.detector.process_console_errors(console_errs, state.url, s.steps)
        new_bugs.extend(s.detector.process_network_errors(network_errs, state.url, s.steps))

        # Only run content-level detectors if page loaded successfully
        if not page_blocked:
            new_bugs.extend(s.detector.process_page_content(state, s.steps))
            new_bugs.extend(s.detector.process_count_consistency(state, s.steps))
            new_bugs.extend(s.detector.process_css_indicators(state, s.steps))
            new_bugs.extend(s.detector.process_broken_images(state, s.steps))
            new_bugs.extend(s.detector.process_accessibility(state, s.steps))
            new_bugs.extend(s.detector.process_seo(state, s.steps))
            new_bugs.extend(s.detector.process_mixed_content(state, s.steps))

        if state.toast_messages:
            new_bugs.extend(s.detector.process_toast_network_crosscheck(
                state.toast_messages, network_errs, state.url, s.steps
            ))

        # Check links
        link_results = await s.browser.check_links(state.links)
        new_bugs.extend(s.detector.process_dead_links(link_results, state.url, s.steps))

        # Performance
        perf = await s.browser.get_performance()
        new_bugs.extend(s.detector.process_performance(perf, state.url, s.steps))

        # Screenshot
        if new_bugs:
            page_name = state.url.split("/")[-1] or "index"
            ss_path = await _auto_screenshot(s, f"crawl_{page_name}", f"Crawl: {state.url}")
            for bug in new_bugs:
                bug.screenshot_path = ss_path

        s.bugs.extend(new_bugs)
        page_results.append((state.url, len(new_bugs)))

        # Discover new internal links to visit (deduplicated by path)
        for link in state.links:
            href = link.get("href", "")
            if link.get("isInternal") and _normalize(href) not in visited_paths and href not in to_visit:
                to_visit.append(href)

    lines = [f"Crawl complete: {len(visited)} pages visited, {len(s.bugs)} total bugs"]
    lines.append("")
    for url, count in page_results:
        marker = f" ({count} bugs)" if count else ""
        lines.append(f"  {url}{marker}")
    lines.append(f"\nTotal bugs in session: {len(s.bugs)}")
    return "\n".join(lines)


# ── Compound Action Tools ─────────────────────────────────────────


@mcp.tool()
async def test_action(element_index: int, action_description: str) -> str:
    """Click a button/link and automatically verify what changed.

    Captures state before and after the click, runs all detectors, computes
    a diff, and takes a screenshot. Returns a complete analysis in one call.

    Args:
        element_index: The [N] index of the element to click
        action_description: What you expect to happen (e.g. "delete the Buy groceries task")
    """
    s = _require_session()
    if element_index < 0 or element_index >= len(s._last_elements):
        return f"Error: element index {element_index} out of range (0-{len(s._last_elements) - 1})"

    el = s._last_elements[element_index]
    label = el.text or el.aria_label or el.placeholder or f"{el.tag}#{el.id or '?'}"
    step = f'test_action: Click "{label}" ({action_description})'
    s.steps.append(step)

    # Drain pre-existing errors
    s.browser.drain_errors()

    # Before state
    before = await s.browser.get_state()

    # Execute click
    ok = await s.browser.click(element_index, s._last_elements)
    if not ok:
        ss = await _auto_screenshot(s, "action_failed", step)
        return f"ACTION FAILED: Could not click \"{label}\" — element may be obscured or gone.\nScreenshot: {ss}"

    await asyncio.sleep(0.3)  # settle time for JS re-renders

    # After state
    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)

    # Errors from this action
    console_errs, network_errs = s.browser.drain_errors()

    # Run detectors
    new_bugs = await _run_all_detectors(s, after, console_errs, network_errs)

    # Compute diff
    changes = compute_changes(before, after, action_description)

    # Screenshot
    ss_path = await _auto_screenshot(s, f"action_{label[:20]}", step)
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

    # Format result
    lines = [
        f'ACTION: Clicked "{label}" <{el.tag}> [{element_index}]',
        f'  ({action_description})',
        "",
        "CHANGES:",
    ]
    for c in changes:
        lines.append(f"  {c}")
    lines.append("")

    if console_errs or network_errs:
        lines.append("ERRORS:")
        for err in console_errs[:3]:
            lines.append(f"  [CONSOLE] {err['text'][:80]}")
        for err in network_errs[:3]:
            lines.append(f"  [HTTP {err['status']}] {err['method']} {err['url'][:60]}")
    else:
        lines.append("ERRORS: None")

    if new_bugs:
        lines.append(f"\nBUGS ({len(new_bugs)} new, {len(s.bugs)} total):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")
    else:
        lines.append(f"\nBUGS: None new. Total: {len(s.bugs)}")

    lines.append(f"\nScreenshot: {ss_path}")
    return "\n".join(lines)


@mcp.tool()
async def test_form(
    form_fields: dict,
    submit_text: str = "",
    submit_index: int = -1,
    expected_result: str = "success",
) -> str:
    """Fill a form, submit it, and verify the result — all in one call.

    Matches fields by name/placeholder, types values, clicks submit, then
    checks if the operation succeeded or failed as expected.

    Args:
        form_fields: Dict mapping field name to value. Example: {"email": "test@test.com", "password": "abc123"}
        submit_text: Text of submit button to click. Auto-detects if empty.
        submit_index: Element index of submit button. Used if submit_text is empty.
        expected_result: "success" (expect redirect/success msg), "validation_error" (expect error msg), or "any"
    """
    s = _require_session()

    # Drain pre-existing errors
    s.browser.drain_errors()
    before = await s.browser.get_state()
    before_url = before.url

    field_results = []
    elements = s._last_elements

    # Fill each field
    for field_key, value in form_fields.items():
        # Re-fetch elements (SPA re-renders may shift indices)
        state = await s.browser.get_state()
        elements = state.elements
        s._last_elements = elements

        idx = s.browser.find_element_by_field(field_key, elements)
        if idx is None:
            field_results.append(f"[MISS] \"{field_key}\" — no matching element found")
            continue

        el = elements[idx]
        if el.tag == "select":
            ok = await s.browser.select_option(idx, str(value), elements)
        else:
            ok = await s.browser.type_text(idx, str(value), elements)

        if ok:
            field_results.append(f"[OK] \"{field_key}\" -> \"{value}\"")
            s.steps.append(f'Type "{value}" into {field_key}')
        else:
            field_results.append(f"[FAIL] \"{field_key}\" — could not type")

    # Find submit button
    state = await s.browser.get_state()
    elements = state.elements
    s._last_elements = elements
    submit_idx = None

    if submit_text:
        for el in elements:
            if el.tag in ("button", "input") and el.text and submit_text.lower() in el.text.lower():
                submit_idx = el.index
                break
    elif submit_index >= 0:
        submit_idx = submit_index
    else:
        # Auto-detect submit
        submit_keywords = ["submit", "save", "create", "sign in", "log in", "login", "register", "send", "add"]
        for el in elements:
            if el.tag in ("button", "input") and el.type in ("submit", None, "button"):
                el_text = (el.text or "").lower()
                if any(kw in el_text for kw in submit_keywords) or el.type == "submit":
                    submit_idx = el.index
                    break

    if submit_idx is None:
        # Fallback: press Enter on the last filled input field
        last_filled = None
        for fr in field_results:
            if "[OK]" in fr:
                # Re-find the last successfully filled field
                for key in reversed(list(form_fields.keys())):
                    idx = s.browser.find_element_by_field(key, elements)
                    if idx is not None:
                        last_filled = idx
                        break
                break
        if last_filled is not None:
            el = elements[last_filled]
            selector = s.browser._build_selector(el)
            try:
                await s.browser._page.press(selector, "Enter", timeout=5000)
                await s.browser._page.wait_for_load_state("networkidle", timeout=10000)
                submit_label = "Enter key"
                s.steps.append("Press Enter to submit")
            except Exception:
                ss = await _auto_screenshot(s, "form_no_submit", "No submit button, Enter failed")
                return f"FORM: Filled {len(field_results)} fields but no submit button found and Enter key failed.\n" + "\n".join(field_results)
        else:
            ss = await _auto_screenshot(s, "form_no_submit", "Could not find submit button")
            return f"FORM: Filled {len(field_results)} fields but could not find submit button.\n" + "\n".join(field_results)

    if submit_idx is not None:
        submit_el = elements[submit_idx]
        submit_label = submit_el.text or "submit"
        s.steps.append(f'Click "{submit_label}"')

        # Click submit
        ok = await s.browser.click(submit_idx, elements)
        if not ok:
            ss = await _auto_screenshot(s, "form_submit_fail", "Submit click failed")
            return f"FORM: Filled fields but submit click failed.\n" + "\n".join(field_results)

    await asyncio.sleep(0.3)

    # After state
    after = await s.browser.get_state()
    s._last_elements = after.elements
    if after.url not in s.pages_visited:
        s.pages_visited.append(after.url)

    console_errs, network_errs = s.browser.drain_errors()
    new_bugs = await _run_all_detectors(s, after, console_errs, network_errs)
    changes = compute_changes(before, after, "form submission")

    # Analyze result
    redirected = after.url != before_url
    has_success_toast = any(
        any(kw in t.lower() for kw in ["success", "saved", "created", "added", "logged", "welcome"])
        for t in after.toast_messages
    )
    has_error_toast = any(
        any(kw in t.lower() for kw in ["error", "fail", "invalid", "required", "incorrect"])
        for t in after.toast_messages
    )
    has_error_text = any(
        kw in after.page_text.lower()
        for kw in ["error", "invalid", "required", "do not match", "failed"]
    )
    server_error = any(e["status"] >= 500 for e in network_errs)

    if expected_result == "success":
        if redirected or has_success_toast:
            outcome = "CONFIRMED — success indicators detected"
        elif server_error:
            outcome = "FAILED — server returned error"
        elif has_error_text:
            outcome = "FAILED — error messages visible on page"
        else:
            outcome = "UNCLEAR — no obvious success or error indicators"
    elif expected_result == "validation_error":
        if has_error_text or has_error_toast:
            outcome = "CONFIRMED — validation errors shown"
        elif redirected or has_success_toast:
            outcome = "UNEXPECTED — form accepted input that should have been rejected"
        else:
            outcome = "UNCLEAR — no obvious validation feedback"
    else:
        outcome = "Observed (no expectation set)"

    ss_path = await _auto_screenshot(s, "form_result", f"Form: {submit_label}")
    for bug in new_bugs:
        bug.screenshot_path = ss_path
    s.bugs.extend(new_bugs)

    lines = [
        f"FORM SUBMISSION via \"{submit_label}\"",
        "",
        "FIELDS:",
    ]
    for fr in field_results:
        lines.append(f"  {fr}")
    lines.append("")
    lines.append("RESULT:")
    lines.append(f"  URL: {before_url} -> {after.url} {'(redirected)' if redirected else '(same page)'}")
    if after.toast_messages:
        lines.append(f"  Toasts: {', '.join(after.toast_messages[:3])}")
    lines.append(f"  Expected: {expected_result} -> {outcome}")
    lines.append("")
    lines.append("CHANGES:")
    for c in changes:
        lines.append(f"  {c}")

    if new_bugs:
        lines.append(f"\nBUGS ({len(new_bugs)} new, {len(s.bugs)} total):")
        for bug in new_bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title[:80]}")
    else:
        lines.append(f"\nBUGS: None new. Total: {len(s.bugs)}")

    lines.append(f"\nScreenshot: {ss_path}")
    return "\n".join(lines)


@mcp.tool()
async def test_crud(
    create_url: str,
    list_url: str,
    item_data: dict,
    item_name_field: str = "",
) -> str:
    """Test a complete Create → Verify → Edit → Verify → Delete → Verify cycle.

    Navigates to the create form, fills it, submits, then verifies the item
    exists on the list page. Then finds edit/delete buttons and tests those too.

    Args:
        create_url: URL of the create/new form (e.g. "/tasks/new")
        list_url: URL where created items appear (e.g. "/tasks")
        item_data: Dict of field:value pairs for creating the item
        item_name_field: Key in item_data that identifies the item (e.g. "title"). Auto-detects if empty.
    """
    s = _require_session()

    # Determine item name
    if item_name_field and item_name_field in item_data:
        item_name = str(item_data[item_name_field])
    else:
        item_name = str(list(item_data.values())[0]) if item_data else "test item"

    results = []
    phase_bugs = []

    # ── PHASE 1: CREATE ──
    results.append("CREATE:")
    try:
        await s.browser.goto(create_url)
        state = await s.browser.get_state()
        s._last_elements = state.elements
        s.steps.append(f"Navigate to {create_url}")

        # Fill fields
        for field_key, value in item_data.items():
            state = await s.browser.get_state()
            s._last_elements = state.elements
            idx = s.browser.find_element_by_field(field_key, state.elements)
            if idx is not None:
                el = state.elements[idx]
                if el.tag == "select":
                    await s.browser.select_option(idx, str(value), state.elements)
                else:
                    await s.browser.type_text(idx, str(value), state.elements)
                s.steps.append(f'Type "{value}" into {field_key}')
                results.append(f"  [OK] Filled \"{field_key}\" = \"{value}\"")
            else:
                results.append(f"  [MISS] Could not find field \"{field_key}\"")

        # Find and click submit
        state = await s.browser.get_state()
        s._last_elements = state.elements
        submit_idx = None
        for el in state.elements:
            if el.tag in ("button", "input") and el.type in ("submit", None, "button"):
                el_text = (el.text or "").lower()
                if any(kw in el_text for kw in ["submit", "save", "create", "add"]) or el.type == "submit":
                    submit_idx = el.index
                    break

        if submit_idx is not None:
            await s.browser.click(submit_idx, state.elements)
            await asyncio.sleep(0.3)
            results.append(f"  [OK] Submitted form")
            s.steps.append("Click submit")
        else:
            results.append(f"  [FAIL] Could not find submit button")

        # Verify on list page
        await s.browser.goto(list_url)
        verify_state = await s.browser.get_state()
        s._last_elements = verify_state.elements
        if _text_in_state(item_name, verify_state):
            results.append(f"  [OK] \"{item_name}\" found on {list_url}")
        else:
            results.append(f"  [FAIL] \"{item_name}\" NOT found on {list_url}")

        await _auto_screenshot(s, "crud_create", f"CRUD create: {item_name}")
    except Exception as e:
        results.append(f"  [ERROR] {str(e)[:100]}")

    # ── PHASE 2: EDIT ──
    results.append("")
    results.append("EDIT:")
    edited_name = f"{item_name} (edited)"
    try:
        state = await s.browser.get_state()
        s._last_elements = state.elements
        edit_idx = s.browser.find_button_near_item(item_name, ["edit", "update", "modify"], state.elements)

        if edit_idx is not None:
            await s.browser.click(edit_idx, state.elements)
            await asyncio.sleep(0.3)
            results.append(f"  [OK] Found and clicked edit button")

            # Type new value into the name field
            state = await s.browser.get_state()
            s._last_elements = state.elements
            name_idx = s.browser.find_element_by_field(
                item_name_field or list(item_data.keys())[0], state.elements
            )
            if name_idx is not None:
                await s.browser.type_text(name_idx, edited_name, state.elements)
                results.append(f"  [OK] Changed to \"{edited_name}\"")

                # Submit edit
                state = await s.browser.get_state()
                s._last_elements = state.elements
                for el in state.elements:
                    if el.tag in ("button", "input") and el.type in ("submit", None, "button"):
                        el_text = (el.text or "").lower()
                        if any(kw in el_text for kw in ["save", "update", "submit"]) or el.type == "submit":
                            await s.browser.click(el.index, state.elements)
                            await asyncio.sleep(0.3)
                            results.append(f"  [OK] Submitted edit")
                            break

                # Verify edit persisted
                await s.browser.goto(list_url)
                verify_state = await s.browser.get_state()
                s._last_elements = verify_state.elements
                if _text_in_state(edited_name, verify_state):
                    results.append(f"  [OK] \"{edited_name}\" found on {list_url}")
                else:
                    results.append(f"  [BUG] \"{edited_name}\" NOT found — edit may not have persisted!")
                    phase_bugs.append(Bug(
                        type=BugType.STATE_VERIFICATION, severity=Severity.HIGH,
                        title=f"Edit did not persist: \"{edited_name}\" not found after save",
                        description=f"Edited item to \"{edited_name}\" and saved, but the new value is not on {list_url}",
                        url=list_url, steps_to_reproduce=list(s.steps),
                    ))
            else:
                results.append(f"  [SKIP] Could not find name field to edit")
        else:
            results.append(f"  [SKIP] No edit button found near \"{item_name}\"")

        await _auto_screenshot(s, "crud_edit", f"CRUD edit: {item_name}")
    except Exception as e:
        results.append(f"  [ERROR] {str(e)[:100]}")

    # ── PHASE 3: DELETE ──
    results.append("")
    results.append("DELETE:")
    _cur = await s.browser.get_state()
    search_name = edited_name if _text_in_state(edited_name, _cur) else item_name
    try:
        state = await s.browser.get_state()
        s._last_elements = state.elements
        del_idx = s.browser.find_button_near_item(search_name, ["delete", "remove", "trash"], state.elements)

        if del_idx is not None:
            before_delete = await s.browser.get_state()
            await s.browser.click(del_idx, state.elements)
            await asyncio.sleep(0.5)
            results.append(f"  [OK] Clicked delete button")

            # Check for confirmation dialog
            state = await s.browser.get_state()
            s._last_elements = state.elements
            for el in state.elements:
                el_text = (el.text or "").lower()
                if any(kw in el_text for kw in ["confirm", "yes", "ok", "sure"]):
                    await s.browser.click(el.index, state.elements)
                    await asyncio.sleep(0.3)
                    results.append(f"  [OK] Confirmed deletion")
                    break

            # Verify deletion
            await s.browser.goto(list_url)
            verify_state = await s.browser.get_state()
            s._last_elements = verify_state.elements
            if not _text_in_state(search_name, verify_state):
                results.append(f"  [OK] \"{search_name}\" is GONE from {list_url}")
            else:
                results.append(f"  [BUG] \"{search_name}\" still present — delete did not persist!")
                phase_bugs.append(Bug(
                    type=BugType.STATE_VERIFICATION, severity=Severity.HIGH,
                    title=f"Delete did not persist: \"{search_name}\" still present after refresh",
                    description=f"Deleted \"{search_name}\" but it reappeared on {list_url}",
                    url=list_url, steps_to_reproduce=list(s.steps),
                ))
        else:
            results.append(f"  [SKIP] No delete button found near \"{search_name}\"")

        await _auto_screenshot(s, "crud_delete", f"CRUD delete: {search_name}")
    except Exception as e:
        results.append(f"  [ERROR] {str(e)[:100]}")

    # Add bugs
    for bug in phase_bugs:
        ss_path = await _auto_screenshot(s, "crud_bug", bug.title[:30])
        bug.screenshot_path = ss_path
    s.bugs.extend(phase_bugs)

    # Summary
    results.append("")
    passed = sum(1 for r in results if "[OK]" in r)
    failed = sum(1 for r in results if "[BUG]" in r or "[FAIL]" in r)
    results.append(f"SUMMARY: {passed} passed, {failed} failed, {len(phase_bugs)} bugs detected")
    results.append(f"Total session bugs: {len(s.bugs)}")
    return "\n".join(results)


@mcp.tool()
async def end_session() -> str:
    """End the testing session, close the browser, and generate an HTML error report.

    Returns the path to the generated report and a summary of findings.
    """
    s = _require_session()

    # Final error drain
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

    duration = asyncio.get_event_loop().time() - (s.start_time or 0)

    result = ExplorationResult(
        url=s.url or "",
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
    """Entry point for argus-mcp command."""
    mcp.run()


if __name__ == "__main__":
    main()
