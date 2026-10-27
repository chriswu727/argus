import asyncio
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from argus.access import AccessPolicy
from argus.domain.contracts import OperationError
from argus.measurement import timed
from argus.security import _redact as _redact
from argus.security import _redact_headers as _redact_headers
from argus.security import redact_body

from .domain.models import InteractiveElement, PageState
from .runtime import BoundedEventLog
from .settings import CaptureSettings


def _capture_body(raw: bytes, headers: dict, cap: int = 16 * 1024) -> Optional[str]:
    """Decode a response body for the agent — text/json only, redacted, capped.

    Redaction runs on the FULL decoded body before truncation, so a secret that
    straddles the size cap can't leak its prefix. Binary payloads are skipped.
    """
    if not raw:
        return None
    ctype = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ctype = (v or "").lower()
            break
    texty = ("json", "text/", "xml", "javascript", "x-www-form-urlencoded")
    if ctype and not any(t in ctype for t in texty):
        return None  # image/font/video/octet-stream/etc — not human-readable
    if not ctype and b"\x00" in raw[:2048]:
        return None  # no content-type + NUL bytes -> almost certainly binary
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    text = _redact(text)
    if len(text) > cap:
        text = text[:cap] + f"\n…[truncated, {len(raw)} bytes total]"
    return text


def _response_body_capture_reason(
    headers: dict, resource_type: str, status: int, limits: CaptureSettings
) -> Optional[str]:
    ctype = next(
        (
            str(value).lower()
            for key, value in (headers or {}).items()
            if key.lower() == "content-type"
        ),
        "",
    )
    length = next(
        (str(value) for key, value in (headers or {}).items() if key.lower() == "content-length"),
        "",
    )
    texty = ("json", "text/", "xml", "javascript", "x-www-form-urlencoded")
    if ctype and not any(token in ctype for token in texty):
        return f"non-text content type {ctype.split(';', 1)[0]}"
    if resource_type not in {"xhr", "fetch"} and not (
        resource_type == "document" and status >= 400
    ):
        return f"resource type {resource_type or 'unknown'} is not inspectable API content"
    try:
        if length and int(length) > limits.read_bytes:
            return f"content-length exceeds {limits.read_bytes} bytes"
    except ValueError:
        return "invalid content-length header"
    if not length and not limits.unknown_length_body:
        return "missing content-length; set ARGUS_CAPTURE_UNKNOWN_LENGTH_BODY=1 to inspect it"
    return None


# JS snippet to extract visible interactive elements from the page.
# Walks open shadow roots too: querySelectorAll does not cross shadow
# boundaries, so a plain document query is blind to Web Components. We
# recurse into every reachable open shadowRoot and tag those elements
# with shadow:true (the resolver/selector layer needs to know, because
# Playwright's :has-text engine doesn't pierce shadow — see _build_selector).
_EXTRACT_ELEMENTS_JS = (
    files("argus.adapters.scripts").joinpath("extract_elements_js.js").read_text()
)

# JS snippet to extract full page content for smart detection.
_EXTRACT_PAGE_CONTENT_JS = (
    files("argus.adapters.scripts").joinpath("extract_page_content_js.js").read_text()
)

# JS snippet to deeply inspect one element. Returns rendered styles (the
# things humans actually perceive: colour, contrast, size, position,
# stacking, truncation), accessibility metadata, and outer HTML. Used by
# the inspect_element tool when the agent suspects a visual / a11y bug
# on a specific surface.
_INSPECT_ELEMENT_JS = files("argus.adapters.scripts").joinpath("inspect_element_js.js").read_text()

_FIND_VISIBLE_ELEMENT_JS = (
    files("argus.adapters.scripts").joinpath("find_visible_element_js.js").read_text()
)

_CHECK_LAYOUT_JS = files("argus.adapters.scripts").joinpath("check_layout_js.js").read_text()

# JS snippet for performance metrics (on-demand).
_EXTRACT_PERFORMANCE_JS = (
    files("argus.adapters.scripts").joinpath("extract_performance_js.js").read_text()
)


class BrowserDriver:
    """Wraps Playwright to drive a browser and capture errors."""

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        capture_settings: Optional[CaptureSettings] = None,
        access_policy: Optional[AccessPolicy] = None,
    ):
        self.headless = headless
        self.capture_settings = capture_settings or CaptureSettings.from_env()
        self.access_policy = access_policy or AccessPolicy.from_env()
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        error_cap = self.capture_settings.error_events
        self.console_errors = BoundedEventLog[Dict](error_cap)
        self.network_errors = BoundedEventLog[Dict](error_cap)
        # Downloads triggered this session (Export CSV / Download invoice / PDF).
        # Playwright Download objects — resolved to name+bytes lazily on query so
        # the agent can VERIFY an export actually fired and isn't empty/corrupt.
        self._downloads = BoundedEventLog[object](self.capture_settings.download_events)
        # Full request/response log — every HTTP call this session has
        # made, regardless of status. Used by network_requests / network_request
        # tools so the agent can verify "did the right /api/foo get called
        # with the right payload" without manually scraping anything.
        self.network_log = BoundedEventLog[Dict](self.capture_settings.network_events)
        # Request-mock routes registered by the agent. {pattern: {response_dict}}.
        self._mock_routes: Dict[str, Dict] = {}
        # Live route handlers, kept out of _mock_routes (which is surfaced to the
        # agent) so mocks can be suspended/restored around a clean re-load.
        self._mock_handlers: Dict[str, object] = {}
        # JS dialog handling — agent queues a response, the handler pops one
        # for each fired alert/confirm/prompt, falls back to auto-dismiss.
        self._dialog_queue: List[Dict] = []
        self._dialog_log = BoundedEventLog[Dict](self.capture_settings.dialog_events)

    # -- lifecycle --

    async def start(self):
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context = await self._browser.new_context(
                viewport=self.viewport, accept_downloads=True
            )
            self._context.on("page", self._on_new_page)
            self._page = await self._context.new_page()
            self._attach_page_listeners(self._page)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()
            self._page = self._context = self._browser = self._playwright = None

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    @property
    def has_page(self) -> bool:
        return self._page is not None

    def page_urls(self) -> list[str]:
        return [self._tab_url(page) for page in self._live_pages()]

    def _tab_url(self, page: Page) -> str:
        url = page.url
        try:
            self.access_policy.check_url(url)
        except (OperationError, OSError, ValueError):
            return "[restricted tab]"
        return _redact(url)

    def selector_for(self, element: InteractiveElement) -> str:
        return self._build_selector(element)

    async def evaluate_js(self, code: str):
        return await self._page.evaluate(code)

    async def click_resolved(self, index: int, elements: list[InteractiveElement]) -> None:
        await self._locator(index, elements).click(timeout=5000)

    @timed("browser_wait")
    async def settle(self) -> None:
        await self._page.wait_for_load_state("networkidle", timeout=10_000)

    async def fill_selector(self, selector: str, value: str, timeout: int = 5000) -> None:
        await self._page.fill(selector, value, timeout=timeout)

    async def select_selector(self, selector: str, value: str, timeout: int = 5000) -> None:
        await self._page.select_option(selector, value, timeout=timeout)

    async def set_checked_resolved(
        self, index: int, elements: list[InteractiveElement], checked: bool
    ) -> None:
        # Radios share a name, so a selector alone would hit the group's first
        # option; _locator keeps the resolved element's identity.
        await self._locator(index, elements).set_checked(checked, timeout=5000)

    async def click_selector(self, selector: str, timeout: int = 5000) -> None:
        await self._page.click(selector, timeout=timeout)

    async def keyboard_press(self, key: str) -> None:
        await self._page.keyboard.press(key)

    # -- listeners --

    def _on_new_page(self, page: Page):
        # Pages spawned via target.click() or window.open() arrive here.
        # The first page (created in start()) is attached directly, so guard
        # against double-attach by checking listener count is zero.
        try:
            already = bool(getattr(page, "_argus_attached", False))
        except Exception:
            already = False
        if not already:
            self._attach_page_listeners(page)

    def _attach_page_listeners(self, page: Page):
        page.on("console", lambda message, source=page: self._on_console(message, source))
        page.on("pageerror", lambda error, source=page: self._on_page_error(error, source))
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        try:
            page._argus_attached = True
        except Exception:
            pass

    async def _on_dialog(self, dialog):
        """Handle alert/confirm/prompt. Use the next queued response if
        the agent set one, otherwise auto-dismiss (Playwright's default
        behaviour, but we record it so get_errors / dialog_log surfaces
        the silent dismissal)."""
        spec = self._dialog_queue.pop(0) if self._dialog_queue else None
        action = (spec or {}).get("action", "dismiss")
        text = (spec or {}).get("text", "")
        self._dialog_log.append(
            {
                "type": dialog.type,
                "message": dialog.message,
                "responded_with": action if spec else "auto-dismiss",
                "text": text,
                "timestamp": datetime.now().isoformat(),
            }
        )
        try:
            if action == "accept":
                await dialog.accept(text)
            else:
                await dialog.dismiss()
        except Exception:
            pass

    def queue_dialog_response(self, action: str, text: str = "") -> None:
        self._dialog_queue.append({"action": action, "text": text})

    def accept_next_dialog(self) -> Dict:
        """Accept the next dialog ahead of any queued responses; returns the
        token to pass to discard_dialog_response if no dialog fires."""
        spec = {"action": "accept", "text": ""}
        self._dialog_queue.insert(0, spec)
        return spec

    def discard_dialog_response(self, spec: Dict) -> None:
        self._dialog_queue = [item for item in self._dialog_queue if item is not spec]

    def dialog_cursor(self) -> int:
        return self._dialog_log.cursor()

    def dialogs_since(self, cursor: int) -> List[Dict]:
        return self._dialog_log.since(cursor)

    def dialog_log_snapshot(self) -> List[Dict]:
        return list(self._dialog_log)

    def _setup_listeners(self):
        # Back-compat shim: old call site that wired listeners onto self._page.
        self._attach_page_listeners(self._page)

    def _on_console(self, msg, page=None):
        if msg.type in ("error", "warning"):
            source_page = getattr(msg, "page", None) or page
            source_url = getattr(source_page, "url", "") or (self._page.url if self._page else "")
            self.console_errors.append(
                {
                    "type": msg.type,
                    "text": msg.text,
                    "url": source_url,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    def _on_page_error(self, error, page=None):
        self.console_errors.append(
            {
                "type": "exception",
                "text": str(error),
                "url": page.url if page is not None else (self._page.url if self._page else ""),
                "timestamp": datetime.now().isoformat(),
            }
        )

    def _on_request(self, request):
        try:
            try:
                page_url = request.url if request.resource_type == "document" else request.frame.url
            except Exception:
                page_url = self._page.url if self._page else ""
            entry = {
                "id": id(request),
                "url": _redact(request.url),
                "method": request.method,
                "resource_type": request.resource_type,
                "headers": _redact_headers(dict(request.headers)),
                "post_data": redact_body(
                    request.post_data or "", request.headers.get("content-type", "")
                )
                or None,
                "started_at": datetime.now().isoformat(),
                "page_url": _redact(page_url or (self._page.url if self._page else "")),
                "status": None,
                "response_headers": None,
                "response_size": None,
            }
            self.network_log.append(entry)
        except Exception:
            pass

    async def _on_response(self, response):
        try:
            req_id = id(response.request)
            entry = next(
                (e for e in reversed(self.network_log) if e.get("id") == req_id),
                None,
            )
            if entry is not None:
                entry["status"] = response.status
                try:
                    raw_headers = dict(response.headers)
                    entry["response_headers"] = _redact_headers(raw_headers)
                except Exception:
                    raw_headers = {}
                    entry["response_headers"] = None
                try:
                    omitted = _response_body_capture_reason(
                        raw_headers,
                        response.request.resource_type,
                        response.status,
                        self.capture_settings,
                    )
                    if omitted:
                        length = next(
                            (
                                value
                                for key, value in raw_headers.items()
                                if key.lower() == "content-length"
                            ),
                            None,
                        )
                        entry["response_size"] = (
                            int(length) if length and str(length).isdigit() else None
                        )
                        entry["response_body"] = None
                        entry["response_body_omitted"] = omitted
                    else:
                        body = await response.body()
                        entry["response_size"] = len(body) if body else 0
                        entry["response_body"] = _capture_body(
                            body, raw_headers, self.capture_settings.body_bytes
                        )
                except Exception:
                    entry["response_size"] = None
                    entry["response_body"] = None
                entry["finished_at"] = datetime.now().isoformat()
        except Exception:
            pass

        if response.status >= 400:
            try:
                page_url = (
                    response.url
                    if response.request.resource_type == "document"
                    else response.request.frame.url
                )
            except Exception:
                page_url = self._page.url if self._page else ""
            self.network_errors.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": response.request.method,
                    "page_url": page_url,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    def _on_request_failed(self, request):
        """A request that errored with NO HTTP response (net::ERR_*, blocked,
        connection refused, DNS). Response-status detection never sees these, so
        a genuinely broken resource stays invisible — capture it with the failure
        reason. Skip net::ERR_ABORTED: that's usually a cancelled navigation or
        prefetch, not a broken resource."""
        try:
            failure = ""
            try:
                failure = request.failure or ""
            except Exception:
                failure = ""
            if "ERR_ABORTED" in failure:
                return
            try:
                page_url = request.url if request.resource_type == "document" else request.frame.url
            except Exception:
                page_url = self._page.url if self._page else ""
            self.network_errors.append(
                {
                    "url": request.url,
                    "status": None,
                    "method": request.method,
                    "page_url": page_url,
                    "failure": failure or "request failed (no response)",
                    "resource_type": getattr(request, "resource_type", None),
                    "timestamp": datetime.now().isoformat(),
                }
            )
        except Exception:
            pass

    def _on_download(self, download):
        """A download fired (Export CSV / Download invoice / PDF report). Store
        the Playwright Download object; its file is resolved to name+bytes lazily
        in downloads_snapshot so the agent can verify the export isn't
        empty/corrupt — a common blind spot no other Argus surface catches."""
        try:
            self._downloads.append(download)
        except Exception:
            pass

    async def downloads_snapshot(self, preview_bytes: int = 2000) -> List[Dict]:
        """Resolve captured downloads to {filename, size, url, preview}. Reads the
        saved file so the agent can confirm content (header-only CSV, 0-byte PDF)."""
        out: List[Dict] = []
        for dl in self._downloads:
            entry: Dict = {}
            try:
                entry["filename"] = dl.suggested_filename
            except Exception:
                entry["filename"] = None
            try:
                entry["url"] = dl.url
            except Exception:
                entry["url"] = None
            try:
                path = await dl.path()  # local temp path Playwright saved it to
                with open(path, "rb") as fh:
                    data = fh.read()
                entry["size"] = len(data)
                try:
                    entry["preview"] = data[:preview_bytes].decode("utf-8", "replace")
                except Exception:
                    entry["preview"] = None
            except Exception as e:
                entry["size"] = None
                entry["preview"] = None
                entry["error"] = str(e)[:120]
            out.append(entry)
        return out

    async def clear_client_storage(self) -> bool:
        """Wipe localStorage + sessionStorage for the current origin, so a
        following reload reflects only SERVER-persisted state — a "Save" that
        only wrote client storage will NOT survive (that's the bug to catch).
        Caveat: apps that keep an auth token in localStorage will log out, which
        the receipt's login-wall guard treats as inconclusive (never certifies)."""
        try:
            await self._page.evaluate(
                "() => { try { localStorage.clear(); } catch(e){} "
                "try { sessionStorage.clear(); } catch(e){} }"
            )
            return True
        except Exception:
            return False

    # -- network mocking --

    async def add_route(
        self,
        pattern: str,
        status: int = 200,
        body: str = "",
        headers: Optional[Dict[str, str]] = None,
        content_type: str = "application/json",
    ) -> None:
        """Register a mock for any request matching `pattern` (URL glob or
        regex string). Subsequent matching requests get the canned response
        instead of hitting the network."""

        async def _handler(route):
            try:
                await route.fulfill(
                    status=status,
                    content_type=content_type,
                    headers=headers or {},
                    body=body,
                )
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await self._page.route(pattern, _handler)
        self._mock_routes[pattern] = {
            "status": status,
            "content_type": content_type,
            "headers": headers or {},
            "body_preview": body[:200],
        }
        self._mock_handlers[pattern] = _handler

    async def remove_route(self, pattern: str) -> bool:
        """Drop a previously-registered mock. Returns True if it existed."""
        if pattern in self._mock_routes:
            try:
                await self._page.unroute(pattern)
            except Exception:
                pass
            self._mock_routes.pop(pattern, None)
            self._mock_handlers.pop(pattern, None)
            return True
        return False

    async def suspend_mocks(self) -> List[str]:
        """Temporarily unroute every active mock; return the patterns suspended.

        The reproduction re-check needs a genuinely clean load. Leaving the
        agent's own forced responses live would let a self-inflicted symptom
        (e.g. an injected 500) re-fire on reload and certify itself
        'reproduced'. Pair with restore_mocks to put the session back.
        """
        suspended = list(self._mock_handlers)
        for pattern in suspended:
            handler = self._mock_handlers.get(pattern)
            try:
                if handler is not None:
                    await self._page.unroute(pattern, handler)
                else:
                    await self._page.unroute(pattern)
            except Exception:
                pass
        return suspended

    async def restore_mocks(self, patterns: List[str]) -> None:
        """Re-register mocks suspended by suspend_mocks (best-effort)."""
        for pattern in patterns:
            handler = self._mock_handlers.get(pattern)
            if handler is not None:
                try:
                    await self._page.route(pattern, handler)
                except Exception:
                    pass

    async def clear_routes(self) -> int:
        """Drop all mocks. Returns the count cleared."""
        count = len(self._mock_routes)
        for pattern in list(self._mock_routes):
            await self.remove_route(pattern)
        return count

    def network_log_snapshot(self) -> List[Dict]:
        """Return a shallow copy of the captured request/response log."""
        return list(self.network_log)

    def network_cursor(self) -> int:
        return self.network_log.cursor()

    def network_events_since(self, cursor: int) -> List[Dict]:
        return self.network_log.since(cursor)

    def error_cursors(self) -> Tuple[int, int]:
        return self.console_errors.cursor(), self.network_errors.cursor()

    def errors_since(
        self, console_cursor: int, network_cursor: int
    ) -> Tuple[List[Dict], List[Dict]]:
        return (
            self.console_errors.since(console_cursor),
            self.network_errors.since(network_cursor),
        )

    def capture_drops(self) -> Dict[str, int]:
        return {
            "network": self.network_log.dropped,
            "console_errors": self.console_errors.dropped,
            "network_errors": self.network_errors.dropped,
            "downloads": self._downloads.dropped,
            "dialogs": self._dialog_log.dropped,
        }

    def clear_network_log(self) -> int:
        n = len(self.network_log)
        self.network_log.clear()
        return n

    # -- storage state --

    async def cookies_get(self, url: Optional[str] = None) -> List[Dict]:
        """Return cookies for the given URL (or all cookies if None)."""
        try:
            urls = [url] if url else None
            return await self._context.cookies(urls=urls)
        except Exception:
            return []

    async def cookies_set(self, cookies: List[Dict]) -> int:
        """Set a list of cookies on the current context. Each cookie
        must have at least name + value + (url OR domain+path)."""
        try:
            await self._context.add_cookies(cookies)
            return len(cookies)
        except Exception:
            return 0

    async def cookies_clear(self) -> bool:
        try:
            await self._context.clear_cookies()
            return True
        except Exception:
            return False

    async def storage_get(self, kind: str = "local") -> Dict[str, str]:
        """Return all key/value pairs in localStorage (kind='local') or
        sessionStorage (kind='session') of the current page."""
        store = "localStorage" if kind == "local" else "sessionStorage"
        try:
            return await self._page.evaluate(
                f"() => {{const r = {{}}; "
                f"for (let i = 0; i < {store}.length; i++) "
                f"{{const k = {store}.key(i); r[k] = {store}.getItem(k);}} "
                f"return r;}}"
            )
        except Exception:
            return {}

    async def storage_set(self, key: str, value: str, kind: str = "local") -> bool:
        store = "localStorage" if kind == "local" else "sessionStorage"
        try:
            await self._page.evaluate(
                f"({{k, v}}) => {store}.setItem(k, v)",
                {"k": key, "v": value},
            )
            return True
        except Exception:
            return False

    async def storage_remove(self, key: str, kind: str = "local") -> bool:
        store = "localStorage" if kind == "local" else "sessionStorage"
        try:
            await self._page.evaluate(
                f"(k) => {store}.removeItem(k)",
                key,
            )
            return True
        except Exception:
            return False

    async def storage_clear(self, kind: str = "local") -> bool:
        store = "localStorage" if kind == "local" else "sessionStorage"
        try:
            await self._page.evaluate(f"() => {store}.clear()")
            return True
        except Exception:
            return False

    # -- state capsules (full client identity snapshot) --

    async def capsule_capture(self) -> Dict:
        """Snapshot the full client-side state: cookies + both web storages + url."""
        return {
            "url": self._page.url if self._page else "",
            "cookies": await self.cookies_get(),
            "local": await self.storage_get("local"),
            "session": await self.storage_get("session"),
        }

    async def capsule_apply(self, capsule: Dict) -> Dict:
        """Restore a captured capsule as a CLEAN REPLACE of the current identity.

        Existing cookies + web storage are cleared first so a prior identity
        can't bleed into the restored one (a merged session can read 'live'
        while actually being a mix). Cookies go on the context; web storage is
        written only once we've confirmed the post-nav page is on the capsule's
        origin (a protected URL may redirect to a cross-origin SSO host).

        Returns counts of what was actually applied vs expected, so the caller
        can warn on a silent shortfall.
        """

        url = capsule.get("url") or ""
        expected_origin = self.access_policy.storage_scope(url)
        applied = {
            "cookies": 0,
            "cookies_expected": 0,
            "local": 0,
            "local_expected": 0,
            "session": 0,
            "session_expected": 0,
            "origin_ok": True,
        }
        try:
            await self.cookies_clear()
        except Exception:
            pass
        cookies = capsule.get("cookies") or []
        applied["cookies_expected"] = len(cookies)
        if cookies:
            applied["cookies"] = await self.cookies_set(cookies)
        await self.goto(url)
        try:
            actual_origin = self.access_policy.storage_scope(self._page.url) if self._page else None
        except (ValueError, OperationError, OSError):
            actual_origin = None
        if actual_origin != expected_origin:
            # Redirected cross-origin (e.g. to an IdP) — writing storage here
            # would land it on the wrong origin. Bail rather than corrupt.
            applied["origin_ok"] = False
            return applied
        for kind in ("local", "session"):
            items = capsule.get(kind) or {}
            applied[f"{kind}_expected"] = len(items)
            try:
                await self.storage_clear(kind)
            except Exception:
                pass
            for k, v in items.items():
                if await self.storage_set(k, v, kind):
                    applied[kind] += 1
        await self.goto(url)
        return applied

    # -- multi-tab --

    def _live_pages(self) -> List[Page]:
        if self._context is None:
            return []
        return [p for p in self._context.pages if not p.is_closed()]

    async def tabs_list(self) -> List[Dict]:
        """Return one entry per open tab: index, url, title, active."""
        out: List[Dict] = []
        for i, p in enumerate(self._live_pages()):
            url = self._tab_url(p)
            title = ""
            try:
                if url != "[restricted tab]":
                    title = await p.title()
            except Exception:
                pass
            url = self._tab_url(p)
            if url == "[restricted tab]":
                title = ""
            out.append(
                {
                    "index": i,
                    "url": url,
                    "title": _redact(title),
                    "active": p is self._page,
                }
            )
        return out

    async def tabs_switch(self, index: int) -> bool:
        pages = self._live_pages()
        if index < 0 or index >= len(pages):
            return False
        self.access_policy.check_url(pages[index].url)
        self._page = pages[index]
        try:
            await self._page.bring_to_front()
        except Exception:
            pass
        return True

    async def tabs_close(self, index: int) -> bool:
        pages = self._live_pages()
        if index < 0 or index >= len(pages):
            return False
        target = pages[index]
        was_active = target is self._page
        try:
            await target.close()
        except Exception:
            return False
        if was_active:
            remaining = self._live_pages()
            self._page = remaining[0] if remaining else None
            if self._page is not None:
                try:
                    await self._page.bring_to_front()
                except Exception:
                    pass
        return True

    # -- waits --

    @timed("browser_wait")
    async def wait_for_text(self, text: str, timeout_s: float = 10.0) -> bool:
        """Poll until the given text appears anywhere in the page body."""
        try:
            await self._page.wait_for_function(
                "(target) => document.body && document.body.innerText.includes(target)",
                arg=text,
                timeout=int(timeout_s * 1000),
            )
            return True
        except Exception:
            return False

    @timed("browser_wait")
    async def wait_for_request(
        self,
        url_substring: str,
        method: Optional[str] = None,
        timeout_s: float = 10.0,
    ) -> Optional[Dict]:
        """Wait for a request whose URL contains `url_substring` (and
        method matches if given). Returns a dict snapshot or None on
        timeout."""
        method_u = method.upper() if method else None

        def match(req):
            if url_substring not in req.url:
                return False
            if method_u and req.method.upper() != method_u:
                return False
            return True

        try:
            req = await self._page.wait_for_event(
                "request",
                predicate=match,
                timeout=int(timeout_s * 1000),
            )
        except Exception:
            return None
        return {
            "url": req.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "post_data": _redact(req.post_data or "") or None,
        }

    # -- navigation --

    @timed("browser_wait")
    async def _settle_network(self, page=None, timeout: int = 6_000):
        """Best-effort wait for network to go idle. A never-resolving fetch (a
        stuck 'Loading…' spinner) means networkidle NEVER fires, so this must
        never block long or throw — the caller proceeds with the loaded DOM."""
        try:
            await (page or self._page).wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    async def goto(self, url: str):
        self.access_policy.check_url(url)
        if self._page is None:
            # All tabs were closed; reopen one so navigation recovers the
            # session instead of dereferencing a None page.
            self._page = await self._context.new_page()
            self._attach_page_listeners(self._page)
        # Returns the main-frame Response so callers can detect 4xx/5xx (a fresh
        # load that errored must not certify a symptom by its absence).
        # domcontentloaded ALWAYS fires once the DOM is parsed and still returns
        # the response. Then BEST-EFFORT wait for networkidle so SPAs that render
        # after fetches are captured — but bounded: a never-resolving fetch (a
        # stuck "Loading…" spinner) means networkidle never fires, so we time out
        # and proceed with the loaded DOM. That makes the stuck state OBSERVABLE
        # (and flaggable as a bug) instead of a 30s hang reported as a load failure.
        resp = await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self._settle_network()
        self.access_policy.check_url(self._page.url)
        return resp

    # -- state extraction --

    @timed("observation")
    async def get_state(self, page=None) -> PageState:
        page = page or self._page
        if page is None:
            raise RuntimeError(
                "No open page — all tabs were closed. Call navigate(url) or "
                "start_session(url) to recover."
            )
        # A click can trigger a navigation that's still in flight; extracting the
        # DOM mid-navigation raises "Execution context was destroyed". Wait for the
        # new document and retry once, so a nav-causing click reads the NEW page
        # instead of surfacing a raw Playwright error to the agent.
        for attempt in range(2):
            try:
                elements = await self._extract_elements(page)
                content = await self._extract_page_content(page)
                break
            except Exception as e:
                if attempt == 0 and "execution context was destroyed" in str(e).lower():
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                    except Exception:
                        pass
                    continue
                raise
        return PageState(
            url=page.url,
            title=await page.title(),
            elements=elements,
            page_text=content.get("pageText", ""),
            toast_messages=[t["text"] for t in content.get("toasts", []) if t.get("visible")],
            counts=content.get("counts", {}),
            css_indicators=[
                f"{ind['cls']}:{ind['text']}" for ind in content.get("cssIndicators", [])
            ],
            item_lists=content.get("itemLists", {}),
            links=content.get("links", []),
            images=content.get("images", []),
            meta_tags=content.get("metaTags", {}),
            headings=content.get("headings", []),
            accessibility_issues=content.get("a11yIssues", []),
            mixed_content=content.get("mixedContent", []),
            open_modals=content.get("openModals", []),
            canvases=content.get("canvases", []),
            focused=content.get("focused"),
            viewport=content.get("viewport"),
        )

    async def inspect_element(self, selector: str) -> Dict:
        """Return computed styles, ARIA metadata and outerHTML for one element.

        `selector` is a Playwright-flavoured selector built from a resolved
        InteractiveElement (use BrowserDriver._build_selector). Resolves
        to the first matching element handle and evaluates the inspect
        snippet against it, so Playwright-specific syntax like
        ``button:has-text("Login")`` works.
        """
        try:
            locator = self._page.locator(selector).first
            handle = await locator.element_handle(timeout=2000)
            if handle is None:
                return {"found": False}
            return await self._page.evaluate(_INSPECT_ELEMENT_JS, handle)
        except Exception:
            return {"found": False}

    async def inspect_visible_element(self, query: str, kind: str = "") -> Dict:
        """Inspect visible content, including headings, text, and images."""
        try:
            return await self._page.evaluate(
                _FIND_VISIBLE_ELEMENT_JS, {"query": query, "kind": kind}
            )
        except Exception:
            return {"found": False}

    async def check_layout(self) -> Dict:
        """Return bounded, read-only layout signals without arbitrary page JS."""
        try:
            return await self._page.evaluate(_CHECK_LAYOUT_JS)
        except Exception:
            return {}

    async def _extract_elements(self, page=None) -> List[InteractiveElement]:
        pg = page or self._page
        raw = await pg.evaluate(_EXTRACT_ELEMENTS_JS)
        elements = [InteractiveElement(**el) for el in raw]
        # The in-page JS traversal reads SAME-ORIGIN iframes (contentDocument) but
        # a CROSS-ORIGIN frame it can only mark. Playwright itself can read those
        # per-frame — and hosted card fields (Stripe/Adyen), OAuth consent, and
        # embedded checkout ARE cross-origin, the highest-stakes surfaces a senior
        # QA prioritizes. Extract them per-frame and tag with the iframe selector
        # so click/type land in the frame (interaction already handles framed els).
        try:
            extra, covered = await self._extract_cross_origin_frames(pg)
            if extra:
                elements = [
                    e for e in elements if not (e.tag == "iframe" and (e.href or "") in covered)
                ]
                elements += extra
                for i, el in enumerate(elements):
                    el.index = i
        except Exception:
            pass
        return elements

    async def _extract_cross_origin_frames(self, pg):
        """Run the element extractor inside each CROSS-ORIGIN child frame (which
        the in-page JS could not reach) and tag those elements with the iframe
        selector. Returns (elements, covered_srcs) — covered_srcs lets the caller
        drop the now-redundant cross-origin markers."""
        from urllib.parse import urlsplit

        def origin(u):
            try:
                s = urlsplit(u or "")
                return (s.scheme, s.hostname, s.port)
            except Exception:
                return None

        main = pg.main_frame
        main_o = origin(main.url)
        extra, covered = [], set()
        for fr in pg.frames:
            if fr is main or origin(fr.url) == main_o:
                continue  # main or same-origin: the in-page JS already covered it
            try:
                handle = await fr.frame_element()
                info = await handle.evaluate(
                    "el => ({sel: el.id ? ('iframe[id='+JSON.stringify(el.id)+']')"
                    " : (el.name ? ('iframe[name='+JSON.stringify(el.name)+']')"
                    " : (el.getAttribute('src') ? ('iframe[src='+JSON.stringify(el.getAttribute('src'))+']') : null)),"
                    " src: el.getAttribute('src')})"
                )
            except Exception:
                info = None
            if not info or not info.get("sel"):
                continue
            try:
                raw = await fr.evaluate(_EXTRACT_ELEMENTS_JS)
            except Exception:
                continue
            for el in raw:
                el["frame"] = info["sel"]
                extra.append(InteractiveElement(**el))
            if info.get("src"):
                covered.add(info["src"])
        return extra, covered

    async def _extract_page_content(self, page=None) -> Dict:
        try:
            return await (page or self._page).evaluate(_EXTRACT_PAGE_CONTENT_JS)
        except Exception:
            return {}

    # -- actions --

    def _locator(self, element_index: int, elements: List[InteractiveElement], page=None):
        """Locator for one element, disambiguating duplicate selectors by order.

        _build_selector yields the same selector for several extracted elements
        when they share identity attrs (N identical 'Delete' buttons all become
        button:has-text("Delete")). page.click(selector) would silently act on
        the first DOM match, discarding the specific row/ordinal element the
        resolver deliberately chose — a silent wrong-action. Count how many
        earlier extracted elements build the same selector and target that nth
        match, so the action lands on the element the caller actually resolved.
        Elements are extracted in document order, so this aligns with the DOM
        match order. For a unique selector nth==0, identical to the old path.
        """
        el = elements[element_index]
        selector = self._build_selector(el)
        # nth must be scoped to the SAME frame — an identical selector in a
        # different frame is a different element, not an earlier duplicate.
        nth = sum(
            1
            for other in elements[:element_index]
            if other.frame == el.frame and self._build_selector(other) == selector
        )
        base = page or self._page
        # Extraction only lists visible elements, so count among visible DOM
        # matches too: otherwise a hidden duplicate (a hover-only Delete in
        # another row) takes the nth slot and the action lands on it.
        visible = f"{selector} >> visible=true"
        if el.frame:
            # Element lives inside one (or more, ' >>> '-chained) iframes — reach
            # it through frame_locator so click/type/inspect land in the frame.
            fl = base
            for part in el.frame.split(" >>> "):
                fl = fl.frame_locator(part)
            return fl.locator(visible).nth(nth)
        return base.locator(visible).nth(nth)

    async def replay(self, start_url: str, actions: List[Dict]) -> Dict:
        """Re-drive a recorded action trace from a cold start and report what the
        page looked like BEFORE the steps and AFTER, so the caller can require
        the symptom to FLIP because of the journey (not pre-exist).

        Runs in an ISOLATED context (a fresh new_context seeded with a copy of
        the live context's storage_state for auth), so re-driven navigation does
        not disturb the agent's live page/DOM. NOTE: the steps still re-execute
        real writes against the shared backend (re-clicking Save/Delete/Add re-
        performs those side effects) — that is inherent to replaying a journey.

        Returns {steps, diverged, baseline_state, final_state}. diverged=True
        (a step could not be re-resolved/applied, or a load returned 4xx/5xx)
        means the path is no longer the same -> INCONCLUSIVE, never certified.
        The isolated context is always closed.
        """
        from .resolver import resolve_element

        if any(action.get("requires_input") or action.get("value_ref") for action in actions):
            return {
                "steps": [{"ok": False, "reason": "replay inputs must be resolved in memory"}],
                "diverged": True,
                "baseline_state": None,
                "final_state": None,
            }
        try:
            storage = await self._context.storage_state()
        except Exception:
            storage = None
        ctx = await self._browser.new_context(viewport=self.viewport, storage_state=storage)
        # Count writes the replay re-executes against the backend, so the caller
        # can warn the agent that re-driving the journey re-performs side effects.
        writes: List[str] = []
        ctx.on(
            "request",
            lambda req: (
                writes.append(req.method)
                if (req.method or "").upper() in ("POST", "PUT", "PATCH", "DELETE")
                else None
            ),
        )
        page = await ctx.new_page()
        steps: List[Dict] = []
        diverged = False
        baseline = None
        try:
            if start_url:
                try:
                    self.access_policy.check_url(start_url)
                    resp = await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
                    await self._settle_network(page)
                    self.access_policy.check_url(page.url)
                    if resp is not None and resp.status >= 400:
                        return {
                            "steps": [
                                {
                                    "act": f"goto {start_url}",
                                    "ok": False,
                                    "reason": f"HTTP {resp.status}",
                                }
                            ],
                            "diverged": True,
                            "baseline_state": None,
                            "final_state": None,
                        }
                except Exception as e:
                    return {
                        "steps": [{"act": f"goto {start_url}", "ok": False, "reason": str(e)[:80]}],
                        "diverged": True,
                        "baseline_state": None,
                        "final_state": None,
                    }
            baseline = await self.get_state(page)
            for act in actions:
                tool = act.get("tool")
                self.access_policy.check_url(page.url)
                desc = act.get("description") or ""
                val = act.get("value")
                if tool == "navigate":
                    try:
                        self.access_policy.check_url(val)
                        resp = await page.goto(val, wait_until="domcontentloaded", timeout=30_000)
                        await self._settle_network(page)
                        self.access_policy.check_url(page.url)
                        if resp is not None and resp.status >= 400:
                            steps.append(
                                {
                                    "act": f"navigate {val}",
                                    "ok": False,
                                    "reason": f"HTTP {resp.status}",
                                }
                            )
                            diverged = True
                            break
                        steps.append({"act": f"navigate {val}", "ok": True})
                    except Exception as e:
                        steps.append({"act": f"navigate {val}", "ok": False, "reason": str(e)[:80]})
                        diverged = True
                        break
                    continue
                if tool == "double_click" and str(val or "").startswith("text:"):
                    try:
                        await page.get_by_text(str(val)[5:], exact=True).dblclick(timeout=5_000)
                        await self._settle_network(page)
                        steps.append({"act": f"{tool} {desc!r}", "ok": True})
                    except Exception as e:
                        steps.append(
                            {
                                "act": f"{tool} {desc!r}",
                                "ok": False,
                                "reason": f"{type(e).__name__}: replay action failed",
                            }
                        )
                        diverged = True
                        break
                    continue
                kind = {"type_into": "input", "select_into": "select"}.get(tool)
                elements = await self._extract_elements(page)
                r = resolve_element(desc, elements, kind_filter=kind, strict_kind=bool(kind))
                if r.reason != "unique" or r.found is None:
                    steps.append(
                        {"act": f"{tool} {desc!r}", "ok": False, "reason": f"re-resolve {r.reason}"}
                    )
                    diverged = True
                    break
                try:
                    loc = self._locator(elements.index(r.found), elements, page=page)
                    if tool == "click_what":
                        await loc.click(timeout=5_000)
                        await self._settle_network(page)
                    elif tool == "type_into":
                        await loc.fill(val or "", timeout=5_000)
                    elif tool == "select_into":
                        await loc.select_option(val or "", timeout=5_000)
                    elif tool == "double_click":
                        await loc.dblclick(timeout=5_000)
                        await self._settle_network(page)
                    elif tool == "hover_what":
                        await loc.hover(timeout=5_000)
                    elif tool == "set_checked":
                        await loc.set_checked(str(val).lower() == "true", timeout=5_000)
                        await self._settle_network(page)
                    else:
                        steps.append(
                            {"act": f"{tool} {desc!r}", "ok": False, "reason": "unknown tool"}
                        )
                        diverged = True
                        break
                    steps.append({"act": f"{tool} {desc!r}", "ok": True})
                except Exception as e:
                    steps.append(
                        {
                            "act": f"{tool} {desc!r}",
                            "ok": False,
                            "reason": f"{type(e).__name__}: replay action failed",
                        }
                    )
                    diverged = True
                    break
            self.access_policy.check_url(page.url)
            final = None if diverged else await self.get_state(page)
            return {
                "steps": steps,
                "diverged": diverged,
                "writes": len(writes),
                "baseline_state": baseline,
                "final_state": final,
            }
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    @timed("browser_wait")
    async def _settle_dom(self, cap_ms: int = 1500, quiet_ms: int = 300) -> None:
        """Wait until the DOM stops mutating for `quiet_ms` (capped at `cap_ms`).
        networkidle doesn't cover pure-JS DOM updates that fire after a click —
        a language toggle, a filter, tab content, lazy render — so observe() run
        right after can catch STALE state and the agent falsely reads "nothing
        changed". Bounded so a live ticker/clock page can't hang it."""
        try:
            await self._page.evaluate(
                """(cfg) => new Promise(resolve => {
                    let timer = setTimeout(done, cfg.quiet);
                    const obs = new MutationObserver(() => {
                        clearTimeout(timer); timer = setTimeout(done, cfg.quiet);
                    });
                    function done(){ try{obs.disconnect();}catch(e){} clearTimeout(cap); resolve(); }
                    const cap = setTimeout(done, cfg.cap);
                    obs.observe(document.documentElement,
                        {childList:true, subtree:true, characterData:true, attributes:true});
                })""",
                {"cap": cap_ms, "quiet": quiet_ms},
            )
        except Exception:
            pass  # navigation destroyed the context, eval blocked, etc. — best-effort

    @timed("browser_wait")
    async def _settle_visual(self, cap_ms: int = 1200) -> None:
        try:
            await self._page.evaluate(
                """(cap) => new Promise(async resolve => {
                    try {
                        const animations = document.getAnimations
                            ? document.getAnimations({subtree: true})
                            : [];
                        const finite = animations.filter(animation => {
                            if (animation.playState !== 'running') return false;
                            try {
                                const end = animation.effect.getComputedTiming().endTime;
                                return Number.isFinite(end) && end <= cap * 2;
                            } catch (_) { return false; }
                        });
                        if (finite.length) {
                            await Promise.race([
                                Promise.all(finite.map(animation => animation.finished.catch(() => null))),
                                new Promise(done => setTimeout(done, cap))
                            ]);
                        }
                        let finished = false;
                        const done = () => {
                            if (finished) return;
                            finished = true;
                            resolve();
                        };
                        setTimeout(done, 100);
                        requestAnimationFrame(() => requestAnimationFrame(done));
                    } catch (_) { resolve(); }
                })""",
                cap_ms,
            )
        except Exception:
            pass

    async def click(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        loc = self._locator(element_index, elements)
        try:
            await loc.click(timeout=5_000)
            await self._settle_network()  # best-effort; a click that kicks off a
            await self._settle_dom()  # hanging fetch must NOT read as failed
            return True
        except Exception:
            # A visually-hidden checkbox/radio (opacity:0 restyled — CSS-only tabs,
            # styled toggles, TodoMVC) fails Playwright's actionability hit-test even
            # though it IS the real control a user drives via its label. Toggle it
            # programmatically: el.click() fires click+change, so both :checked-driven
            # CSS (tab panels) and onchange handlers run. Only for these input types —
            # a normal click that failed should stay failed (honest).
            el = elements[element_index]
            if (el.type or "").lower() in ("checkbox", "radio"):
                try:
                    await loc.evaluate("el => el.click()")
                    await self._settle_network()
                    await self._settle_dom()
                    return True
                except Exception:
                    return False
            return False

    async def type_text(
        self, element_index: int, text: str, elements: List[InteractiveElement]
    ) -> bool:
        loc = self._locator(element_index, elements)
        try:
            await loc.fill(text, timeout=5_000)
            return True
        except Exception:
            # Rich-text editors / contenteditable that fill() can't handle: focus,
            # select any existing content, and type via the keyboard.
            try:
                await loc.click(timeout=5_000)
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.type(text)
                await self._settle_dom()
                return True
            except Exception:
                return False

    async def select_option(
        self, element_index: int, value: str, elements: List[InteractiveElement]
    ) -> bool:
        try:
            await self._locator(element_index, elements).select_option(value, timeout=5_000)
            return True
        except Exception:
            return False

    async def double_click(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        try:
            await self._locator(element_index, elements).dblclick(timeout=5_000)
            await self._settle_network()
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def double_click_text(self, text: str) -> Optional[bool]:
        """Double-click the one element whose visible text is exactly `text`.

        Inline editors (TodoMVC labels) listen for dblclick on plain text that
        is not an interactive element. None means no unique exact match, so the
        caller can fall back to resolving an interactive element."""
        if not text.strip():
            return None
        loc = self._page.get_by_text(text.strip(), exact=True)
        try:
            if await loc.count() != 1:
                return None
            await loc.dblclick(timeout=5_000)
            await self._settle_network()
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def hover(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        try:
            await self._locator(element_index, elements).hover(timeout=5_000)
            return True
        except Exception:
            return False

    async def right_click(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        try:
            await self._locator(element_index, elements).click(button="right", timeout=5_000)
            return True
        except Exception:
            return False

    async def press_key(
        self,
        key: str,
        element_index: Optional[int] = None,
        elements: Optional[List[InteractiveElement]] = None,
    ) -> bool:
        """Press a keyboard key (Escape, Enter, Tab, ArrowDown, 'Control+a', ...).
        Focuses `element` first when given (locator.press), else a page-level
        keyboard press. Settles the DOM so a resulting change (modal dismiss,
        submit) is visible on the next observe."""
        try:
            if element_index is not None and elements is not None:
                await self._locator(element_index, elements).press(key, timeout=5_000)
            else:
                await self._page.keyboard.press(key)
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def _point_in_view(self, x: int, y: int):
        """Scroll a viewport point (from observe's canvas coords) into view if
        it's outside the fold, and return the point to actually click — or None
        if nothing is there. page.mouse.click never auto-scrolls and never errors
        on an off-screen point, so a below-fold canvas click would silently miss
        AND report success. This makes coordinate actions land or fail honestly."""
        try:
            info = await self._page.evaluate("() => ({h: window.innerHeight, sy: window.scrollY})")
            h, sy0 = info["h"], info["sy"]
            if y < 0 or y >= h:
                page_y = y + sy0  # observe gives viewport coords; make page-absolute
                await self._page.evaluate("(ty) => window.scrollTo(0, ty)", int(page_y - h / 2))
                sy1 = await self._page.evaluate("() => window.scrollY")
                y = int(page_y - sy1)  # actual viewport y after the (maybe clamped) scroll
            present = await self._page.evaluate(
                "(p) => !!document.elementFromPoint(p[0], p[1])", [x, y]
            )
            return (x, y) if present else None
        except Exception:
            return (x, y)  # can't verify (rare) — best-effort proceed

    async def click_at(self, x: int, y: int) -> bool:
        """Real mouse click at viewport pixel (x, y) — the escape hatch for
        canvas/WebGL UIs with no DOM element to target by description."""
        try:
            pt = await self._point_in_view(x, y)
            if pt is None:
                return False
            await self._page.mouse.click(pt[0], pt[1])
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def hover_at(self, x: int, y: int) -> bool:
        try:
            pt = await self._point_in_view(x, y)
            if pt is None:
                return False
            await self._page.mouse.move(pt[0], pt[1])
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def type_at(self, x: int, y: int, text: str) -> bool:
        try:
            pt = await self._point_in_view(x, y)
            if pt is None:
                return False
            await self._page.mouse.click(pt[0], pt[1])  # focus the point, then type
            await self._page.keyboard.type(text)
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def drag_at(self, from_x: int, from_y: int, to_x: int, to_y: int) -> bool:
        """Coordinate drag: mousedown at (from), INCREMENTAL moves to (to),
        mouseup. Incremental moves are required so mouse-based drag libs
        (Sortable.js / Trello / react-beautiful-dnd) register mousemove and
        reorder — a single jump doesn't trigger them. The escape hatch for
        draggables with no DOM marker, plus canvas draw/resize and slider thumbs."""
        try:
            await self._page.mouse.move(from_x, from_y)
            await self._page.mouse.down()
            steps = 12
            for i in range(1, steps + 1):
                await self._page.mouse.move(
                    from_x + (to_x - from_x) * i / steps, from_y + (to_y - from_y) * i / steps
                )
            await self._page.mouse.up()
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def resize(self, width: int, height: int) -> bool:
        """Resize the viewport in-session (keeps page state) so the agent can
        sweep responsive breakpoints — Playwright's set_viewport_size, then settle
        so a media-query relayout (hamburger appears, content reflows) is visible."""
        try:
            await self._page.set_viewport_size({"width": int(width), "height": int(height)})
            self.viewport = {"width": int(width), "height": int(height)}
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def emulate_device(self, device_name: str) -> Dict:
        """Re-open the current page under a real device profile (touch, mobile UA,
        device-scale-factor, viewport) — not just a viewport resize like resize().
        UA/touch/DPR are context-creation options, so this swaps the context;
        cookies + localStorage carry over via storage_state so a logged-in session
        survives. Surfaces mobile-only bugs (touch targets, mobile nav, UA-gated
        content, viewport-meta layout) a desktop pass can't see."""
        try:
            descriptor = (self._playwright.devices or {}).get(device_name)
            if not descriptor:
                names = list((self._playwright.devices or {}).keys())
                return {
                    "ok": False,
                    "reason": f"unknown device {device_name!r}",
                    "examples": [
                        n
                        for n in ("iPhone 13", "Pixel 5", "iPad Pro 11", "Galaxy S9+")
                        if n in names
                    ],
                }
            cfg = {k: v for k, v in descriptor.items() if k != "default_browser_type"}
            url = self._page.url if self._page else None
            try:
                storage = await self._context.storage_state()
            except Exception:
                storage = None
            old = self._context
            self._context = await self._browser.new_context(
                accept_downloads=True, storage_state=storage, **cfg
            )
            self._context.on("page", self._on_new_page)
            self._page = await self._context.new_page()
            self._attach_page_listeners(self._page)
            try:
                await old.close()
            except Exception:
                pass
            if url and not url.startswith("about:"):
                await self.goto(url)
            self.viewport = descriptor.get("viewport") or self.viewport
            return {
                "ok": True,
                "device": device_name,
                "viewport": descriptor.get("viewport"),
                "is_mobile": descriptor.get("is_mobile"),
                "has_touch": descriptor.get("has_touch"),
            }
        except Exception as e:
            return {"ok": False, "reason": str(e)[:150]}

    async def emulate_media(
        self, color_scheme: Optional[str] = None, reduced_motion: Optional[str] = None
    ) -> bool:
        """Emulate a CSS media preference on the live page (no context swap needed —
        page.emulate_media is page-level). Lets a tester see the app in dark mode /
        reduced-motion, where theme bugs (invisible text, unreadable contrast, a
        half-applied theme) hide because you'd normally have to toggle OS settings."""
        kwargs: Dict = {}
        if color_scheme:
            kwargs["color_scheme"] = color_scheme
        if reduced_motion:
            kwargs["reduced_motion"] = reduced_motion
        if not kwargs:
            return False
        try:
            await self._page.emulate_media(**kwargs)
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def drag(
        self,
        source_index: int,
        target_index: int,
        elements: List[InteractiveElement],
    ) -> bool:
        try:
            await self._locator(source_index, elements).drag_to(
                self._locator(target_index, elements),
                timeout=10_000,
            )
            return True
        except Exception:
            return False

    async def upload_file(
        self,
        element_index: int,
        paths: List[str],
        elements: List[InteractiveElement],
    ) -> bool:
        paths = [str(self.access_policy.check_file(path)) for path in paths]
        try:
            await self._locator(element_index, elements).set_input_files(paths, timeout=5_000)
            return True
        except Exception:
            return False

    async def drop_file(
        self,
        element_index: int,
        path: str,
        elements: List[InteractiveElement],
    ) -> bool:
        """Drop a real on-disk file onto a dropzone element (react-dropzone-style
        DnD upload). Builds a DataTransfer with a File carrying the ACTUAL bytes
        and dispatches dragenter/dragover/drop — which upload_file (input-only)
        and drag_what (element-to-element, no files) can't do."""
        import base64
        import mimetypes

        path = str(self.access_policy.check_file(path))
        try:
            data = Path(path).read_bytes()
            name = Path(path).name
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            await self._locator(element_index, elements).evaluate(
                """(el, info) => {
                    const bin = atob(info.b64);
                    const arr = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                    const file = new File([arr], info.name, {type: info.type});
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    for (const t of ['dragenter', 'dragover', 'drop']) {
                        el.dispatchEvent(new DragEvent(t, {bubbles: true, cancelable: true, dataTransfer: dt}));
                    }
                }""",
                {"name": name, "type": mime, "b64": base64.b64encode(data).decode("ascii")},
            )
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def paste_into(
        self,
        element_index: int,
        text: str,
        elements: List[InteractiveElement],
    ) -> bool:
        """Paste `text` into an element via a synthetic paste ClipboardEvent (so
        the app's onpaste handler fires — sanitization, auto-format, OTP split),
        then simulate the default insert if the handler did NOT consume it.
        type_into fires keydown/input, never a paste event, so it can't test this."""
        try:
            await self._locator(element_index, elements).evaluate(
                """(el, text) => {
                    el.focus();
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    const ev = new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: dt});
                    const notPrevented = el.dispatchEvent(ev);
                    if (notPrevented) {
                        const tag = el.tagName;
                        if (tag === 'INPUT' || tag === 'TEXTAREA') {
                            const s = el.selectionStart ?? el.value.length;
                            const e = el.selectionEnd ?? el.value.length;
                            el.value = el.value.slice(0, s) + text + el.value.slice(e);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                        } else if (el.isContentEditable) {
                            document.execCommand('insertText', false, text);
                        }
                    }
                }""",
                text,
            )
            await self._settle_dom()
            return True
        except Exception:
            return False

    async def go_back(self) -> bool:
        try:
            await self._page.go_back(wait_until="domcontentloaded", timeout=10_000)
            await self._settle_network()
            return True
        except Exception:
            return False

    async def scroll_down(self):
        if self._page is None:
            return
        await self._page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(0.5)

    async def scroll_by(self, px: int = 1000):
        """Scroll a larger step (for scanning a long / virtualized list), letting
        lazy-load and virtualization settle before the next read."""
        if self._page is None:
            return
        await self._page.evaluate("(d) => window.scrollBy(0, d)", int(px))
        await asyncio.sleep(0.4)

    @timed("screenshot")
    async def screenshot(self, path: str, full_page: bool = False) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._settle_visual()
        await self._page.screenshot(path=path, full_page=full_page)
        return path

    @timed("screenshot")
    async def element_screenshot(self, selector: str, path: str) -> Optional[str]:
        """Capture just the bounds of one element (Playwright crops natively)."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._settle_visual()
            locator = self._page.locator(selector).first
            handle = await locator.element_handle(timeout=2000)
            if handle is None:
                return None
            await handle.screenshot(path=path)
            return path
        except Exception:
            return None

    # Element-matching helpers used to live here. They've been replaced by
    # argus.resolver.resolve_element, which is backend-agnostic and operates
    # on InteractiveElement records — keeping browser.py focused on
    # Playwright-shaped concerns only.

    # -- link checking --

    async def check_links(self, links: List[Dict]) -> List[Dict]:
        """Check internal link status via HEAD requests (falls back to GET on 405)."""
        results = []
        checked: set = set()
        for link in links:
            href = link.get("href", "")
            if not href or href in checked or not link.get("isInternal"):
                continue
            checked.add(href)
            try:
                self.access_policy.check_url(href)
                resp = await self._context.request.head(href, timeout=5000, max_redirects=0)
                # 405 = server doesn't support HEAD, retry with GET
                if resp.status == 405:
                    resp = await self._context.request.get(href, timeout=5000, max_redirects=0)
                # 403 from context.request often means anti-bot, not a real dead link
                # Mark as ok since the page loaded fine in the browser
                is_ok = None if 300 <= resp.status < 400 else resp.ok or resp.status == 403
                results.append({"href": href, "status": resp.status, "ok": is_ok})
            except OperationError:
                results.append({"href": href, "status": 0, "ok": None})
            except Exception:
                results.append({"href": href, "status": 0, "ok": False})
        return results

    # -- performance --

    async def get_performance(self) -> Dict:
        """Extract performance metrics from the current page."""
        try:
            return await self._page.evaluate(_EXTRACT_PERFORMANCE_JS)
        except Exception:
            return {}

    # -- error draining --

    def drain_errors(self) -> Tuple[List[Dict], List[Dict]]:
        console = self.console_errors.copy()
        network = self.network_errors.copy()
        self.console_errors.clear()
        self.network_errors.clear()
        return console, network

    # -- selector building --

    @staticmethod
    def _build_selector(el: InteractiveElement) -> str:
        if el.id:
            # Attribute form, not "#id": modern framework ids carry ':' (React
            # useId ':r3:', Radix, MUI) or '.', which are CSS-special — a raw
            # "#:r3:" is a parse error (element reported "obscured/stale") and
            # "#ok.x" silently mis-targets. Escape like the other attr branches.
            id_escaped = el.id.replace("\\", "\\\\").replace('"', '\\"')
            return f'[id="{id_escaped}"]'
        if el.name:
            return f'{el.tag}[name="{el.name}"]'
        if el.placeholder:
            ph_escaped = el.placeholder[:60].replace('"', '\\"')
            return f'{el.tag}[placeholder="{ph_escaped}"]'
        if el.aria_label:
            al_escaped = el.aria_label[:60].replace('"', '\\"')
            return f'{el.tag}[aria-label="{al_escaped}"]'
        if el.role:
            return f'{el.tag}[role="{el.role}"]'
        if el.text and el.tag in ("a", "button"):
            # textContent can be multi-line and carry CSS-hostile chars — a
            # card link wraps "[no image]\n Name\n $9.99\n ...". A raw newline
            # inside a CSS string literal is a parse error (BADSTRING), so
            # collapse whitespace to a single line, then escape backslash and
            # quote. has-text matches a (whitespace-normalised) substring, so
            # a clean leading chunk still resolves the element.
            collapsed = " ".join(el.text.split())
            snippet = collapsed[:50]
            text_escaped = snippet.replace("\\", "\\\\").replace('"', '\\"')
            # :has-text doesn't pierce shadow DOM; the standalone text engine
            # does. Use the piercing form only for shadow elements so the
            # light-DOM path keeps its proven substring semantics. text="..."
            # is an EXACT match, so a truncated snippet matches zero elements —
            # only use it when the whole label fits; for longer shadow labels
            # fall back to the unquoted substring form so a clean leading chunk
            # still resolves (mirroring has-text).
            if el.shadow:
                if len(collapsed) <= 50:
                    return f'{el.tag} >> text="{text_escaped}"'
                return f"{el.tag} >> text={snippet}"
            return f'{el.tag}:has-text("{text_escaped}")'
        # Last resort: tag + type
        if el.type and el.tag == "input":
            return f'{el.tag}[type="{el.type}"]'
        return el.tag
