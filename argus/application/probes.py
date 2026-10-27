from __future__ import annotations

from argus.application.context import current_runtime
from argus.domain.contracts import Rejection, ToolText

from .capture import _file_event_bugs
from .coverage import _coverage_reference_url
from .evidence import _auto_screenshot
from .payloads import observation_data
from .progress import Stage
from .session import _require_session
from .text import _present_with_scroll, _resolve_url, _short, _text_in_state
from .verification import _looks_logged_out


async def verify_persistence(
    expect: str,
    target_text: str,
    after_url: str = "",
    clear_storage: bool = False,
    expect_status: int = 0,
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
    your expectation, review the evidence and use suggested_verify with
    record_bug. It rechecks the observed failure by fresh loads, without
    replaying Save/Delete. The suggestion is not a confirmation.

    Args:
        expect: "present" or "absent" — what state the target_text
                should be in after the fresh page load.
        target_text: The text or value you're checking for.
        after_url: Page to load and inspect. Defaults to the current URL.
        clear_storage: When True, wipe localStorage/sessionStorage before the
            reload so you test TRUE server persistence — a "Save" that only wrote
            client storage will read absent (data would be lost on another
            device/browser). Default False keeps client storage (proves
            server-backed truth without logging out). Use True when the feature
            is supposed to persist to a server/account. If clearing logs the app
            out, the result is unreliable — re-check without it.
        expect_status: Optional HTTP status the fresh load should return, such
            as 404 for a deleted item's own page. Absence on an error or login
            page is otherwise inconclusive; presence is always reported.
    """
    s = _require_session()
    expect_norm = (expect or "").strip().lower()
    if expect_norm not in ("present", "absent"):
        return Rejection(
            f"verify_persistence: invalid `expect` {expect!r}. Use 'present' or 'absent'."
        )
    if not target_text.strip():
        return Rejection("verify_persistence: target_text must not be empty.")
    if expect_status and not 100 <= expect_status <= 599:
        return Rejection("verify_persistence: expect_status must be between 100 and 599.")
    await current_runtime().progress(Stage.VERIFYING)

    s.steps.append(
        f"verify_persistence({expect_norm}, {target_text[:60]!r}"
        f"{', clear_storage' if clear_storage else ''})"
    )

    current_url = s.browser.current_url
    nav_url = _resolve_url(s, after_url) if after_url else current_url
    before_state = await s.browser.get_state()
    before_present = _text_in_state(target_text, before_state)
    if not before_present and expect_norm == "present":
        # A just-saved row can sit below the fold or outside a virtualized window.
        before_present = await _present_with_scroll(s, target_text, before_state)
    before_matches = before_present == (expect_norm == "present")
    if clear_storage:
        await s.browser.goto(nav_url)  # land on the right origin
        await s.browser.clear_client_storage()  # then wipe client storage
    response = await s.browser.goto(nav_url)
    after_state = await s.browser.get_state()
    s._last_elements = after_state.elements

    present = await _present_with_scroll(s, target_text, after_state)
    text_matches = present == (expect_norm == "present")
    status = response.status if response is not None else None
    status_matches = (status == expect_status) if expect_status else None
    unreliable_page = _looks_logged_out(after_state) or (
        status is not None and status >= 400 and not status_matches
    )
    # Observed presence and an observed status mismatch are direct evidence;
    # absence on an error or login page may have the wrong cause.
    conclusive = present or not unreliable_page or status_matches is False
    if expect_status and status is None:
        conclusive = False
    matches = (text_matches and status_matches is not False) if conclusive else None
    s.verification_checks.append(
        {
            "expect": expect_norm,
            "target_text": _short(target_text, 200),
            "url": _coverage_reference_url(nav_url),
            "present": present,
            "matches": matches,
            "clear_storage": clear_storage,
            "before_matches": before_matches,
            "conclusive": conclusive,
            "status": status,
            "expect_status": expect_status or None,
            "text_matches": text_matches,
            "status_matches": status_matches,
        }
    )
    s.exploration.last_verified_action = len(s.action_trace)

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
    if status_matches is False:
        verdict = f"HTTP {status}, expected {expect_status}. " + verdict
    if not conclusive:
        verdict = (
            "Target absent on an HTTP error or login page — absence cannot establish "
            "persistence. Pass expect_status if this status is the expected outcome."
        )

    suggested_verify = None
    if conclusive and not text_matches and before_matches and not _looks_logged_out(before_state):
        suggested_verify = {
            "expect": "absent" if expect_norm == "present" else "present",
            "target_text": target_text.strip(),
            "at_url": nav_url,
            "clear_storage": clear_storage,
        }
    guidance = (
        "If this transition proves a user-affecting bug, pass suggested_verify to record_bug. "
        "It rechecks the failure without repeating writes; do not use replay for this check."
        if suggested_verify
        else "Review the observation; a mismatch alone is not a confirmed bug. "
        "Do not repeat writes just to obtain a receipt."
    )
    outcome = "INCONCLUSIVE" if matches is None else "MATCH" if matches else "MISMATCH"
    return ToolText(
        f"verify_persistence(expect={expect_norm!r}, target={target_text[:60]!r}) "
        f"on {nav_url}\n"
        f"  Result: {outcome} — {verdict}\n"
        f"  Decide: {guidance}\n  suggested_verify: {suggested_verify}",
        {
            "verification": dict(s.verification_checks[-1]),
            "observation": observation_data(s, after_state),
            "finding_confirmed": False,
            "suggested_verify": suggested_verify,
            "next_action": guidance,
        },
    )


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
    dead = [r for r in link_results if r["ok"] is False]
    alive = [r for r in link_results if r["ok"]]
    inconclusive = [r for r in link_results if r["ok"] is None]
    external = [link for link in state.links if not link.get("isInternal")]

    lines = [f"Checked {len(link_results)} internal links on {state.url}"]
    lines.append(f"  OK: {len(alive)}")
    lines.append(f"  Dead: {len(dead)}")
    lines.append(f"  Inconclusive (redirect or policy restriction): {len(inconclusive)}")
    lines.append(f"  External (not checked): {len(external)}")
    for result in inconclusive:
        lines.append(f"  [Unverified HTTP {result['status']}] {result['href']}")
    if dead:
        lines.append("")
        for r in dead:
            lines.append(f"  [HTTP {r['status']}] {r['href']}")
        lines.append("")
        lines.append("Decide: are any of these real bugs? Call record_bug if so.")
    return "\n".join(lines)


async def check_performance() -> str:
    """Read raw performance metrics from the browser's Performance API
    (load time, TTFB, request count, large resources).

    Argus does not auto-record bugs here — Lighthouse already owns the
    performance-audit space. Only call record_bug if the page is so slow
    or so heavy that it materially blocks a real user (multi-second TTFB
    on a primary flow, multi-MB hero asset, etc).
    """
    s = _require_session()
    current_url = s.browser.current_url
    perf_data = await s.browser.get_performance()

    nav = perf_data.get("navigation", {})
    summary = perf_data.get("summary", {})
    lines = [f"Performance for {current_url}"]
    if nav:
        lines.append(f"  Load time: {nav.get('loadTime', 0) / 1000:.2f}s")
        lines.append(f"  TTFB: {nav.get('ttfb', 0) / 1000:.2f}s")
        lines.append(f"  DOM interactive: {nav.get('domInteractive', 0) / 1000:.2f}s")
    lines.append(f"  Total requests: {summary.get('totalRequests', '?')}")
    lines.append(f"  Total size: {summary.get('totalSize', 0) / 1024:.0f} KB")
    large = perf_data.get("resources", [])
    if large:
        lines.append("  Large resources (>500KB):")
        for r in large:
            lines.append(f"    {r['size'] / 1024:.0f}KB — {r['name'][:80]}")
    return "\n".join(lines)


async def crawl_site(max_pages: int = 20) -> str:
    """Discover pages: crawl internal links from the current page, auto-capturing
    only console/network events (tagged auto-captured) and checking links per page.

    This is page DISCOVERY, not a judgment pass — it does not decide what's a bug
    for you. Walk the surfaced pages with observe() and record_bug what you confirm.
    Can take 30-120 seconds depending on site size.

    Args:
        max_pages: Maximum number of pages to visit (default 20)
    """
    from urllib.parse import urlparse, urlunparse

    s = _require_session()
    start_url = s.browser.current_url if s.browser.has_page else s.url
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
        recent = s.steps[s._steps_since_last_bug :]
        new_bugs = s.detector.process_console_errors(console_errs, state.url, recent)
        new_bugs.extend(s.detector.process_network_errors(network_errs, state.url, recent))

        # Probe links once (raw probe, no auto-bug — agent decides).
        link_results = await s.browser.check_links(state.links)
        dead = [r for r in link_results if r["ok"] is False]

        # Screenshot when console/network captured something.
        if new_bugs:
            page_name = state.url.split("/")[-1] or "index"
            ss_path = await _auto_screenshot(s, f"crawl_{page_name}", f"Crawl: {state.url}")
            for bug in new_bugs:
                bug.screenshot_path = ss_path

        filed_bugs = _file_event_bugs(s, new_bugs)
        page_results.append((state.url, len(filed_bugs), len(dead)))

        # Discover new internal links to visit (deduplicated by path)
        for link in state.links:
            href = link.get("href", "")
            if (
                link.get("isInternal")
                and _normalize(href) not in visited_paths
                and href not in to_visit
            ):
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
