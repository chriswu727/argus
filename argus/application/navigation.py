from __future__ import annotations

from argus.domain.contracts import ErrorCode, Rejection

from .coverage import _coverage_reference_url
from .evidence import _record_action
from .payloads import action_result
from .session import _require_session, _require_web_session


async def tabs_list() -> str:
    """List every open tab/popup in the current browser context.

    Many real flows spawn a second tab — OAuth, "open in new tab",
    target=_blank links, window.open from ad code. Without this, the
    agent only ever sees the original page and misses bugs in the
    spawned context.
    """
    s = _require_session()
    err = _require_web_session(s, "tabs_list")
    if err:
        return err
    tabs = await s.browser.tabs_list()
    if not tabs:
        return "No tabs."
    lines = [f"{len(tabs)} tab(s):"]
    for t in tabs:
        marker = "* " if t["active"] else "  "
        title = (t["title"] or "")[:60]
        lines.append(f"{marker}[{t['index']}] {title} — {t['url']}")
    lines.append("(* = active. Use tabs_switch(index) to focus another tab.)")
    return "\n".join(lines)


async def tabs_switch(index: int) -> str:
    """Make the tab at the given index the active tab. All subsequent
    observe / click / type / network / storage calls target it."""
    s = _require_session()
    err = _require_web_session(s, "tabs_switch")
    if err:
        return err
    ok = await s.browser.tabs_switch(index)
    if not ok:
        return f"tabs_switch: no tab at index {index}. Call tabs_list to see available tabs."
    # The active page changed — refresh the resolver pool so the next
    # click_what/type_into resolves against THIS tab, not the previous one.
    s._last_elements = (await s.browser.get_state()).elements
    s.steps.append(f"tabs_switch({index})")
    return f"Switched to tab {index}: {s.browser.current_url}. observe() to see its elements."


async def tabs_close(index: int) -> str:
    """Close the tab at the given index. If it was active, focus
    falls back to the first remaining tab."""
    s = _require_session()
    err = _require_web_session(s, "tabs_close")
    if err:
        return err
    ok = await s.browser.tabs_close(index)
    if not ok:
        return f"tabs_close: no tab at index {index}."
    s.steps.append(f"tabs_close({index})")
    if not s.browser.has_page:
        return (
            "Closed the last tab — no open page remains. Call "
            "navigate(url) or start_session(url) to continue."
        )
    # Focus may have moved to another tab — refresh the resolver pool.
    s._last_elements = (await s.browser.get_state()).elements
    return f"Closed tab {index}. Active page is now {s.browser.current_url}."


async def wait_for_text(text: str, timeout_s: float = 10.0) -> str:
    """Block until `text` appears anywhere in the page body, or timeout.

    Use this after triggering an async action when you know what the
    success/failure copy will say ("Saved", "Invalid email"). Cheaper
    and more deterministic than polling observe() in a loop.

    Args:
        text: Substring to look for in document.body.innerText.
        timeout_s: Seconds to wait before giving up (default 10).
    """
    s = _require_session()
    err = _require_web_session(s, "wait_for_text")
    if err:
        return err
    if not text:
        return "wait_for_text: pass a non-empty text."
    found = await s.browser.wait_for_text(text, timeout_s)
    s.steps.append(f"wait_for_text({text!r}, {timeout_s}s) -> {'found' if found else 'timeout'}")
    if found:
        return f"Found: {text!r}"
    return f"Timed out after {timeout_s}s — {text!r} did not appear."


async def wait_for_request(
    url_substring: str,
    method: str = "",
    timeout_s: float = 10.0,
) -> str:
    """Block until the next outgoing request matches the filter, or
    timeout.

    Pattern: trigger an action, then wait for the API call it should
    fire — confirms the front-end actually wired the click to the
    network. Combine with network_request(url_substring=...) to read
    the full payload after.

    Args:
        url_substring: Substring that must appear in the request URL.
        method: Optional HTTP method filter (GET/POST/PUT/DELETE).
        timeout_s: Seconds to wait (default 10).
    """
    s = _require_session()
    err = _require_web_session(s, "wait_for_request")
    if err:
        return err
    if not url_substring:
        return "wait_for_request: pass a url_substring."
    snap = await s.browser.wait_for_request(
        url_substring,
        method or None,
        timeout_s,
    )
    label = f"{method or 'ANY'} ~{url_substring!r}"
    s.steps.append(f"wait_for_request({label}, {timeout_s}s) -> {'matched' if snap else 'timeout'}")
    if snap:
        post = snap.get("post_data") or ""
        if post and len(post) > 200:
            post = post[:197] + "..."
        return (
            f"Matched: {snap['method']} {snap['url']}\n"
            f"  resource_type: {snap['resource_type']}\n"
            f"  post_data: {post or '(none)'}"
        )
    return f"Timed out after {timeout_s}s — no request matched {label}."


async def navigate(url: str) -> str:
    """Navigate to a specific URL.

    Args:
        url: The URL to navigate to
    """
    s = _require_session()
    err = _require_web_session(s, "navigate")
    if err:
        return err
    step = f"Navigate to {url}"
    s.steps.append(step)

    await s.browser.goto(url)
    state = await s.browser.get_state()
    s._last_elements = state.elements
    if state.url not in s.pages_visited:
        s.pages_visited.append(state.url)
    _record_action(s, "navigate", value=url)

    return await action_result(
        s,
        f"Navigated to {state.url} — {state.title} ({len(state.elements)} elements)",
        "navigate",
        state,
    )


async def go_back() -> str:
    """Go back to the previous page."""
    s = _require_session()
    err = _require_web_session(s, "go_back")
    if err:
        return err
    s.steps.append("Go back")

    ok = await s.browser.go_back()
    if ok:
        state = await s.browser.get_state()
        s._last_elements = state.elements
        if state.url not in s.pages_visited:
            s.pages_visited.append(state.url)
        # Recorded for replay and export_test, which would otherwise skip it.
        _record_action(s, "go_back", _coverage_reference_url(state.url))
        return await action_result(s, f"Went back to {state.url}", "back", state)
    return Rejection("Failed to go back", ErrorCode.BACKEND)


async def scroll_down() -> str:
    """Scroll the page down to reveal more content."""
    s = _require_session()
    err = _require_web_session(s, "scroll_down")
    if err:
        return err
    s.steps.append("Scroll down")
    await s.browser.scroll_down()
    _record_action(s, "scroll_down")
    return "Scrolled down. Call observe() to see what's now in view."
