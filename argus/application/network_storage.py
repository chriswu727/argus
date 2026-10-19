from __future__ import annotations

from argus.security import _redact, _redact_headers

from .session import _require_session, _require_web_session


def _filter_network_log(log, url_substring=None, method=None, status_min=None):
    """Slice the network log by simple criteria — used by network_requests
    and network_request to surface what the agent cares about without
    paging through everything."""
    out = log
    if url_substring:
        out = [e for e in out if url_substring in (e.get("url") or "")]
    if method:
        m = method.upper()
        out = [e for e in out if (e.get("method") or "").upper() == m]
    if status_min is not None:
        out = [e for e in out if (e.get("status") or 0) >= status_min]
    return out


async def network_requests(
    url_substring: str = "",
    method: str = "",
    status_min: int = 0,
    limit: int = 30,
) -> str:
    """List the HTTP requests this page has issued, newest last.

    Every request and response is captured in the background; this tool
    drains the current snapshot, optionally filtered. Use it to verify
    "did the right /api/foo get called", "what method", "what response
    status", and to spot requests the UI doesn't surface (analytics,
    background polls, third-party widgets).

    Args:
        url_substring: keep only requests whose URL contains this string.
        method: keep only this method (GET / POST / PUT / …). Empty = all.
        status_min: keep only responses ≥ this status (e.g. 400 to find
                    failures only). 0 = all.
        limit: cap the returned list (default 30).
    """
    s = _require_session()
    err = _require_web_session(s, "network_requests")
    if err:
        return err

    log = s.browser.network_log_snapshot()
    filtered = _filter_network_log(
        log, url_substring or None, method or None, status_min if status_min > 0 else None
    )
    shown = filtered[-limit:]

    if not shown:
        dropped = (
            f" {s.browser.network_log.dropped} oldest request(s) were discarded by the capture limit."
            if s.browser.network_log.dropped
            else ""
        )
        return (
            f"No matching requests "
            f"(filters: url~{url_substring!r}, method={method!r}, "
            f"status≥{status_min}). Total retained this session: {len(log)}.{dropped}"
        )

    lines = [
        f"Network log: {len(shown)} of {len(filtered)} matching ({len(log)} total this session)."
    ]
    if s.browser.network_log.dropped:
        lines.append(
            f"  Capture limit reached: {s.browser.network_log.dropped} oldest request(s) were discarded."
        )
    for e in shown:
        url_display = _redact(e.get("url") or "")[:90]
        status = e.get("status")
        status_s = str(status) if status is not None else "pending"
        size = e.get("response_size")
        size_s = f" {size}B" if size is not None else ""
        lines.append(
            f"  [{status_s:>7}] {(e.get('method') or '?'):<6} "
            f"{e.get('resource_type', ''):<10} {url_display}{size_s}"
        )
    return "\n".join(lines)


async def network_request(
    url_substring: str,
    method: str = "",
) -> str:
    """Get the full request/response detail for ONE captured request.

    Picks the most recent request whose URL contains `url_substring`
    (and matches `method` if given). Returns headers, post-body,
    response status, response headers, response size — everything you
    need to assert "this exact payload was sent and returned 200".

    For the list view, use network_requests.
    """
    s = _require_session()
    err = _require_web_session(s, "network_request")
    if err:
        return err
    if not url_substring:
        return "network_request: pass a url_substring to identify the request."

    log = s.browser.network_log_snapshot()
    matches = _filter_network_log(log, url_substring, method or None)
    if not matches:
        return (
            f"No request matches url~{url_substring!r}"
            f"{', method=' + method if method else ''}. "
            f"Total captured: {len(log)}."
        )

    e = matches[-1]
    lines = [
        f"Request: {e.get('method')} {_redact(e.get('url') or '')}",
        f"  resource_type: {e.get('resource_type')}",
        f"  page_url:      {e.get('page_url')}",
        f"  started:       {e.get('started_at')}",
        "",
        "Request headers:",
    ]
    for k, v in _redact_headers(e.get("headers") or {}).items():
        lines.append(f"  {k}: {v}")
    if e.get("post_data"):
        # The request body of a login POST is the plaintext password — redact.
        body = _redact(e["post_data"])
        lines.append("")
        lines.append("Request body:")
        lines.append("  " + (body[:1500] + ("…[truncated]" if len(body) > 1500 else "")))

    lines.append("")
    status = e.get("status")
    if status is None:
        lines.append("Response: (still pending — request fired but no response yet)")
    else:
        lines.append(f"Response: HTTP {status}")
        size = e.get("response_size")
        if size is not None:
            lines.append(f"  body size: {size} bytes")
        if e.get("finished_at"):
            lines.append(f"  finished:  {e['finished_at']}")
        rh = _redact_headers(e.get("response_headers") or {})
        if rh:
            lines.append("  Response headers:")
            for k, v in rh.items():
                lines.append(f"    {k}: {v}")
        rbody = e.get("response_body")
        if rbody:
            lines.append("")
            lines.append("Response body:")
            # The whole point: read what the server actually returned — a 200
            # whose body says {"error": ...} is the deception behind a toast.
            lines.append(rbody)
        elif e.get("response_body_omitted"):
            lines.append(f"  body omitted: {e['response_body_omitted']}")

    return "\n".join(lines)


async def network_mock(
    url_pattern: str,
    status: int = 200,
    body: str = "",
    content_type: str = "application/json",
) -> str:
    """Intercept any request matching `url_pattern` and return a canned
    response, instead of hitting the network.

    Use this to test how the UI handles specific server responses
    (5xx, 401, slow JSON, malformed payload) without needing the
    backend to cooperate.

    Args:
        url_pattern: glob-style (`**/api/users`) or full URL substring.
                     Playwright's page.route() patterns apply.
        status: HTTP status code to return (default 200).
        body: response body string.
        content_type: response Content-Type header.
    """
    s = _require_session()
    err = _require_web_session(s, "network_mock")
    if err:
        return err
    if not url_pattern:
        return "network_mock: pass a url_pattern."
    try:
        await s.browser.add_route(
            pattern=url_pattern,
            status=status,
            body=body,
            content_type=content_type,
        )
    except Exception as exc:
        return f"network_mock failed: {type(exc).__name__}: {exc}"
    s.steps.append(f"network_mock({url_pattern!r}) -> HTTP {status}")
    return (
        f"Mock registered: {url_pattern} -> HTTP {status} "
        f"({content_type}, {len(body)}B body).\n"
        f"Subsequent matching requests will return this canned response.\n"
        f"Call network_unmock or network_clear_mocks to remove."
    )


async def network_unmock(url_pattern: str) -> str:
    """Drop a mock previously registered via network_mock."""
    s = _require_session()
    err = _require_web_session(s, "network_unmock")
    if err:
        return err
    try:
        removed = await s.browser.remove_route(url_pattern)
    except Exception as exc:
        return f"network_unmock failed: {exc}"
    if removed:
        return f"Unmocked: {url_pattern}"
    return f"No mock registered for {url_pattern!r}."


async def network_clear_mocks() -> str:
    """Drop every mock registered this session."""
    s = _require_session()
    err = _require_web_session(s, "network_clear_mocks")
    if err:
        return err
    n = await s.browser.clear_routes()
    return f"Cleared {n} mock(s)."


async def network_clear_log() -> str:
    """Drop the captured request/response log (the mocks themselves stay
    registered). Useful between scenarios to isolate the call set."""
    s = _require_session()
    err = _require_web_session(s, "network_clear_log")
    if err:
        return err
    n = s.browser.clear_network_log()
    return f"Cleared {n} captured request entries."


async def cookies_get(url: str = "") -> str:
    """List cookies on the current browser context.

    Use this to verify what the server / front-end set after login,
    consent banner, A/B opt-in, etc. Pair with cookies_set to seed
    a known-good auth session and skip the login UI for downstream
    tests.

    Args:
        url: Filter to cookies that would be sent for this URL.
             Empty = return every cookie on the context.
    """
    s = _require_session()
    err = _require_web_session(s, "cookies_get")
    if err:
        return err
    cookies = await s.browser.cookies_get(url or None)
    if not cookies:
        return "No cookies." + (f" (filter: url={url})" if url else "")
    lines = [f"{len(cookies)} cookie(s){' for ' + url if url else ''}:"]
    for c in cookies:
        domain = c.get("domain", "")
        path = c.get("path", "/")
        name = c.get("name", "")
        val = c.get("value", "")
        if len(val) > 60:
            val = val[:57] + "..."
        flags = []
        if c.get("httpOnly"):
            flags.append("HttpOnly")
        if c.get("secure"):
            flags.append("Secure")
        if c.get("sameSite"):
            flags.append(f"SameSite={c['sameSite']}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {name}={val}  ({domain}{path}){flag_str}")
    return "\n".join(lines)


async def cookies_set(cookies: list) -> str:
    """Inject one or more cookies into the current browser context.

    Each cookie must be a dict with at least name + value, and either
    a `url` field OR both `domain` + `path`. Common extras: expires
    (unix seconds), httpOnly, secure, sameSite ("Strict"|"Lax"|"None").

    Useful for skipping the login UI: paste a session cookie captured
    out-of-band and the next navigate() request lands authenticated.

    Args:
        cookies: List of cookie dicts. Example:
                 [{"name": "session", "value": "abc",
                   "url": "http://127.0.0.1:5555"}]
    """
    s = _require_session()
    err = _require_web_session(s, "cookies_set")
    if err:
        return err
    if not cookies:
        return "cookies_set: pass a non-empty list of cookie dicts."
    n = await s.browser.cookies_set(cookies)
    s.steps.append(f"cookies_set({n} cookie(s))")
    if n == 0:
        return (
            "cookies_set: 0 set — check that each entry has name + value "
            "and either url OR domain+path."
        )
    return f"Set {n} cookie(s) on the context."


async def cookies_clear() -> str:
    """Clear every cookie on the browser context (logout-all-the-things)."""
    s = _require_session()
    err = _require_web_session(s, "cookies_clear")
    if err:
        return err
    ok = await s.browser.cookies_clear()
    s.steps.append("cookies_clear")
    return "Cleared all cookies." if ok else "cookies_clear: failed."


async def storage_get(kind: str = "local") -> str:
    """Read every key/value in localStorage or sessionStorage of the
    current page.

    Args:
        kind: "local" (default) for localStorage, "session" for
              sessionStorage.
    """
    s = _require_session()
    err = _require_web_session(s, "storage_get")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_get: kind must be 'local' or 'session'."
    items = await s.browser.storage_get(kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    if not items:
        return f"{label}: empty."
    lines = [f"{label} ({len(items)} key(s)):"]
    for k, v in items.items():
        sval = v if v is not None else ""
        if len(sval) > 200:
            sval = sval[:197] + "..."
        lines.append(f"  {k} = {sval}")
    return "\n".join(lines)


async def storage_set(key: str, value: str, kind: str = "local") -> str:
    """Write a single key/value to localStorage or sessionStorage.

    Use this to seed a feature-flag, theme preference, or onboarding-
    completed marker without driving the UI to set it.

    Args:
        key: Storage key.
        value: Value (always stored as a string in browser storage).
        kind: "local" or "session" (default "local").
    """
    s = _require_session()
    err = _require_web_session(s, "storage_set")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_set: kind must be 'local' or 'session'."
    ok = await s.browser.storage_set(key, value, kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_set({label}, {key!r})")
    return f"Set {label}[{key!r}]." if ok else f"storage_set: failed for {key!r}."


async def storage_remove(key: str, kind: str = "local") -> str:
    """Delete a single key from localStorage or sessionStorage."""
    s = _require_session()
    err = _require_web_session(s, "storage_remove")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_remove: kind must be 'local' or 'session'."
    ok = await s.browser.storage_remove(key, kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_remove({label}, {key!r})")
    return f"Removed {label}[{key!r}]." if ok else "storage_remove: failed."


async def storage_clear(kind: str = "local") -> str:
    """Clear all keys from localStorage or sessionStorage on this page."""
    s = _require_session()
    err = _require_web_session(s, "storage_clear")
    if err:
        return err
    if kind not in ("local", "session"):
        return "storage_clear: kind must be 'local' or 'session'."
    ok = await s.browser.storage_clear(kind)
    label = "localStorage" if kind == "local" else "sessionStorage"
    s.steps.append(f"storage_clear({label})")
    return f"Cleared {label}." if ok else "storage_clear: failed."
