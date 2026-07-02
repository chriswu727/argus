from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from typing import Dict, List, Optional, Tuple

from .models import InteractiveElement, PageState

# Cap on how much of a (redacted) response body we keep for inspection.
_BODY_CAP = 16 * 1024

# Field-name fragment that marks a secret-bearing key (matched anywhere in the
# key, so authToken / id_token / sessionId / x-csrf all hit).
_SECRET_KEY = (
    r"(?:[\w-]*(?:password|passwd|secret|token|jwt|bearer|auth|api[_-]?key|"
    r"access[_-]?key|session|sid|csrf|xsrf|credential|cookie)[\w-]*)"
)
_RE_JSON_STR = re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*)"[^"]*"', re.IGNORECASE)
# Non-string secret values (numbers, bare words) — but not objects/arrays/strings.
_RE_JSON_BARE = re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*)(?!["{\[])([^\s,}\]]+)', re.IGNORECASE)
_RE_FORM = re.compile(r'(?i)\b(' + _SECRET_KEY + r')=([^&\s]+)')
_RE_JWT = re.compile(r'eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+')
_RE_BEARER = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]+')

_SENSITIVE_HEADERS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
}


def _redact(text: str) -> str:
    """Mask credentials in a free-text/JSON/form blob before it's surfaced.

    Layered because secrets arrive in many shapes: a generic JWT pattern, JSON
    string + bare values under secret-named keys, form-encoded pairs, and bare
    Bearer tokens. Not a guarantee against every exotic encoding, but it kills
    the common JWT/token/cookie/password leaks.
    """
    if not text:
        return text
    text = _RE_JWT.sub("[redacted-jwt]", text)
    text = _RE_JSON_STR.sub(r'\1"[redacted]"', text)
    text = _RE_JSON_BARE.sub(r"\1[redacted]", text)
    text = _RE_FORM.sub(r"\1=[redacted]", text)
    text = _RE_BEARER.sub(r"\1[redacted]", text)
    return text


def _redact_headers(headers: dict) -> dict:
    """Mask whole values of credential-bearing headers; redact the rest in place."""
    out = {}
    for k, v in (headers or {}).items():
        out[k] = "[redacted]" if k.lower() in _SENSITIVE_HEADERS else _redact(str(v))
    return out


def _capture_body(raw: bytes, headers: dict) -> Optional[str]:
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
    if len(text) > _BODY_CAP:
        text = text[:_BODY_CAP] + f"\n…[truncated, {len(raw)} bytes total]"
    return text

# JS snippet to extract visible interactive elements from the page.
# Walks open shadow roots too: querySelectorAll does not cross shadow
# boundaries, so a plain document query is blind to Web Components. We
# recurse into every reachable open shadowRoot and tag those elements
# with shadow:true (the resolver/selector layer needs to know, because
# Playwright's :has-text engine doesn't pierce shadow — see _build_selector).
_EXTRACT_ELEMENTS_JS = """
() => {
    const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick], [tabindex]:not([tabindex="-1"]), [draggable="true"]';
    const MAX = 400;
    const out = [];
    const seen = new Set();

    function record(el, inShadow) {
        if (seen.has(el)) return;
        seen.add(el);
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0 || style.display === 'none' || style.visibility === 'hidden') return;
        out.push({
            index: out.length,
            tag: el.tagName.toLowerCase(),
            type: el.type || null,
            text: (el.textContent || '').trim().slice(0, 100) || null,
            placeholder: el.placeholder || null,
            href: el.href || null,
            value: el.value || null,
            disabled: el.disabled || false,
            role: el.getAttribute('role') || null,
            // Fall back to the field's VISIBLE label (a person targets a form
            // field by the label they see next to it). Prefer the accessible
            // name (label[for], wrapping <label>, aria-labelledby); then the
            // common unassociated preceding <label> sibling. The missing `for`
            // is still flagged separately as an a11y issue.
            aria_label: el.getAttribute('aria-label') || (function(){
                var t = el.tagName.toLowerCase();
                if (t !== 'input' && t !== 'select' && t !== 'textarea') return null;
                var pick = function(n){ var s = n && n.textContent ? n.textContent.trim() : ''; return s ? s.slice(0,100) : null; };
                if (el.id) { try { var lf = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]'); if (pick(lf)) return pick(lf); } catch (e) {} }
                var w = el.closest('label'); if (pick(w)) return pick(w);
                var lb = el.getAttribute('aria-labelledby'); if (lb) { var t2 = document.getElementById(lb); if (pick(t2)) return pick(t2); }
                var p = el.previousElementSibling;
                while (p) { if (p.tagName === 'LABEL' && pick(p)) return pick(p); if (p.tagName === 'INPUT' || p.tagName === 'SELECT' || p.tagName === 'TEXTAREA') break; p = p.previousElementSibling; }
                return null;
            })() || null,
            name: el.name || null,
            id: el.id || null,
            parent_context: (el.closest('li, tr, .card, .list-item, [class*="item"], [class*="row"]') || {}).textContent?.trim()?.slice(0, 200) || null,
            shadow: inShadow,
        });
    }

    function walk(root, inShadow) {
        if (out.length >= MAX) return;
        root.querySelectorAll(sel).forEach(el => { if (out.length < MAX) record(el, inShadow); });
        // Descend into open shadow roots hosted anywhere under this root.
        root.querySelectorAll('*').forEach(node => {
            if (out.length < MAX && node.shadowRoot) walk(node.shadowRoot, true);
        });
    }

    walk(document, false);
    return out;
}
"""

# JS snippet to extract full page content for smart detection.
_EXTRACT_PAGE_CONTENT_JS = """
() => {
    const result = { pageText: '', toasts: [], counts: {}, cssIndicators: [], itemLists: {},
                     links: [], images: [], metaTags: {}, headings: [], a11yIssues: [], mixedContent: [],
                     openModals: [], focused: null, viewport: null };

    // 1. Full visible text — simple and robust
    try {
        result.pageText = (document.body.innerText || '').slice(0, 5000);
    } catch(e) {}

    // 1a. Open modals / dialogs / popovers
    try {
        const modalSels = '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open], .modal.show, .modal.open';
        document.querySelectorAll(modalSels).forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return;
            const text = (el.textContent || '').trim().slice(0, 300);
            if (!text) return;
            result.openModals.push({
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                ariaLabel: el.getAttribute('aria-label') || '',
                ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                text: text,
                isModal: el.getAttribute('aria-modal') === 'true'
            });
        });
    } catch(e) {}

    // 1b. What's focused right now?
    try {
        const f = document.activeElement;
        if (f && f !== document.body && f.tagName !== 'BODY') {
            result.focused = {
                tag: f.tagName.toLowerCase(),
                type: f.type || null,
                id: f.id || null,
                name: f.name || null,
                ariaLabel: f.getAttribute('aria-label') || null,
                text: (f.textContent || '').trim().slice(0, 80) || null,
                placeholder: f.placeholder || null,
                value: (typeof f.value === 'string' ? f.value.slice(0, 80) : null)
            };
        }
    } catch(e) {}

    // 1c. Viewport vs document height — let the agent know if there's content below the fold
    try {
        result.viewport = {
            scrollY: window.scrollY,
            scrollX: window.scrollX,
            innerHeight: window.innerHeight,
            innerWidth: window.innerWidth,
            documentHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
            documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
            atTop: window.scrollY <= 1,
            atBottom: (window.scrollY + window.innerHeight) >= (Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 1)
        };
    } catch(e) {}

    // 2. Toast/notification messages
    try {
        const sels = '.toast, [role="alert"], .alert-success, .alert-error, .alert-warning, [class*="toast"]';
        document.querySelectorAll(sels).forEach(el => {
            const text = el.textContent.trim();
            if (text) {
                const s = window.getComputedStyle(el);
                result.toasts.push({
                    text: text.slice(0, 200),
                    visible: s.display !== 'none' && s.visibility !== 'hidden',
                    classes: el.className || ''
                });
            }
        });
    } catch(e) {}

    // 3. Number + label counts
    try {
        document.querySelectorAll('.stat, .stat-val, .count, .badge, h1, h2, h3, p, span').forEach(el => {
            const text = el.textContent.trim();
            const m = text.match(/^(\\d+)\\s+(.+)$/);
            if (m) result.counts[m[2].trim()] = parseInt(m[1], 10);
        });
    } catch(e) {}

    // 4. Semantic CSS indicators
    try {
        ['remaining-zero','task-done','error','loading','spinner','alert-error','alert-success'].forEach(cls => {
            document.querySelectorAll('.' + cls).forEach(el => {
                result.cssIndicators.push({
                    cls: cls,
                    text: el.textContent.trim().slice(0, 100),
                    tag: el.tagName.toLowerCase()
                });
            });
        });
    } catch(e) {}

    // 5. Item lists
    try {
        document.querySelectorAll('.card, .list, ul, ol').forEach(container => {
            const items = container.querySelectorAll('.task-item, .list-item, li, tr');
            if (items.length >= 2) {
                const key = (container.className || container.tagName).slice(0, 50);
                result.itemLists[key] = Array.from(items).map(it => it.textContent.trim().slice(0, 200));
            }
        });
    } catch(e) {}

    // 6. All links on page
    try {
        document.querySelectorAll('a[href]').forEach(el => {
            const href = el.href;
            if (!href || href.startsWith('javascript:') || href === '#') return;
            result.links.push({
                href: href,
                text: (el.textContent || '').trim().slice(0, 100),
                isInternal: href.startsWith(window.location.origin)
            });
        });
    } catch(e) {}

    // 7. Images
    try {
        document.querySelectorAll('img').forEach(el => {
            result.images.push({
                src: el.src || el.getAttribute('src') || '',
                alt: el.alt,
                hasAlt: el.hasAttribute('alt'),
                naturalWidth: el.naturalWidth,
                naturalHeight: el.naturalHeight,
                complete: el.complete,
                loaded: el.complete && el.naturalWidth > 0
            });
        });
    } catch(e) {}

    // 8. Meta tags & headings (SEO)
    try {
        const gm = (n) => { const e = document.querySelector('meta[name=\"'+n+'\"], meta[property=\"'+n+'\"]'); return e ? (e.content||'') : ''; };
        result.metaTags = {
            title: document.title || '',
            description: gm('description'),
            ogTitle: gm('og:title'),
            ogDescription: gm('og:description'),
            ogImage: gm('og:image'),
            canonical: (document.querySelector('link[rel=\"canonical\"]') || {}).href || '',
            viewport: gm('viewport'),
            htmlLang: document.documentElement.lang || ''
        };
        document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(el => {
            result.headings.push({ level: parseInt(el.tagName[1]), text: el.textContent.trim().slice(0, 200) });
        });
    } catch(e) {}

    // 9. Accessibility basics
    try {
        document.querySelectorAll('img').forEach(el => {
            if (!el.hasAttribute('alt')) {
                result.a11yIssues.push({type:'img_no_alt', src: (el.src||'').slice(0,100)});
            }
        });
        document.querySelectorAll('input,select,textarea').forEach(el => {
            if (el.type==='hidden'||el.type==='submit'||el.type==='button') return;
            const s = window.getComputedStyle(el);
            if (s.display==='none'||s.visibility==='hidden') return;
            const has = (el.id && document.querySelector('label[for=\"'+el.id+'\"]')) || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title || el.closest('label');
            if (!has) result.a11yIssues.push({type:'input_no_label', tag:el.tagName.toLowerCase(), inputType:el.type||'', name:el.name||'', placeholder:el.placeholder||''});
        });
        document.querySelectorAll('button, a, [role=\"button\"]').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display==='none'||s.visibility==='hidden') return;
            if (!(el.textContent||'').trim() && !el.getAttribute('aria-label') && !el.title && !el.querySelector('img[alt]')) {
                result.a11yIssues.push({type:'no_accessible_name', tag:el.tagName.toLowerCase(), html:el.outerHTML.slice(0,150)});
            }
        });
        if (!document.documentElement.lang) result.a11yIssues.push({type:'no_html_lang'});
    } catch(e) {}

    // 10. Mixed content
    try {
        if (window.location.protocol === 'https:') {
            const ck = (sel, attr) => { document.querySelectorAll(sel).forEach(el => { const u=el.getAttribute(attr)||''; if(u.startsWith('http://')) result.mixedContent.push({url:u.slice(0,200),tag:el.tagName.toLowerCase(),attr:attr}); }); };
            ck('img[src]','src'); ck('script[src]','src'); ck('link[href]','href'); ck('iframe[src]','src'); ck('video[src]','src'); ck('audio[src]','src');
        }
    } catch(e) {}

    return result;
}
"""

# JS snippet to deeply inspect one element. Returns rendered styles (the
# things humans actually perceive: colour, contrast, size, position,
# stacking, truncation), accessibility metadata, and outer HTML. Used by
# the inspect_element tool when the agent suspects a visual / a11y bug
# on a specific surface.
_INSPECT_ELEMENT_JS = """
(el) => {
    if (!el) return { found: false };
    const s = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const truncated =
        (el.scrollWidth > el.clientWidth + 1 && (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.textOverflow === 'ellipsis')) ||
        (el.scrollHeight > el.clientHeight + 1 && (s.overflow === 'hidden' || s.overflowY === 'hidden'));
    const labels = [];
    if (el.id) {
        document.querySelectorAll('label[for="' + el.id + '"]').forEach(lab => {
            labels.push((lab.textContent || '').trim().slice(0, 100));
        });
    }
    return {
        found: true,
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || '').trim().slice(0, 200),
        outerHtml: el.outerHTML.slice(0, 1500),
        styles: {
            color: s.color,
            backgroundColor: s.backgroundColor,
            fontSize: s.fontSize,
            fontWeight: s.fontWeight,
            display: s.display,
            visibility: s.visibility,
            opacity: s.opacity,
            position: s.position,
            zIndex: s.zIndex,
            overflow: s.overflow,
            textOverflow: s.textOverflow,
            cursor: s.cursor,
            border: s.border,
            padding: s.padding,
            margin: s.margin
        },
        rect: {
            x: rect.x, y: rect.y,
            width: rect.width, height: rect.height,
            inViewport: rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth
        },
        truncated: truncated,
        scrollDimensions: { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight },
        ariaLabel: el.getAttribute('aria-label'),
        ariaDescribedby: el.getAttribute('aria-describedby'),
        ariaHidden: el.getAttribute('aria-hidden'),
        role: el.getAttribute('role'),
        title: el.title || null,
        disabled: !!el.disabled,
        readonly: !!el.readOnly,
        labels: labels,
        focused: document.activeElement === el
    };
}
"""

# JS snippet for performance metrics (on-demand).
_EXTRACT_PERFORMANCE_JS = """
() => {
    const result = { navigation: null, resources: [], summary: {} };
    try {
        const nav = performance.getEntriesByType('navigation');
        if (nav.length > 0) {
            const n = nav[0];
            result.navigation = {
                loadTime: n.loadEventEnd - n.startTime,
                domContentLoaded: n.domContentLoadedEventEnd - n.startTime,
                ttfb: n.responseStart - n.startTime,
                domInteractive: n.domInteractive - n.startTime
            };
        }
    } catch(e) {}
    try {
        const resources = performance.getEntriesByType('resource');
        result.summary.totalRequests = resources.length;
        result.summary.totalSize = 0;
        resources.forEach(r => {
            const sz = r.transferSize || 0;
            result.summary.totalSize += sz;
            if (sz > 500 * 1024) result.resources.push({name:r.name.slice(0,200),type:r.initiatorType,size:sz,duration:Math.round(r.duration)});
        });
    } catch(e) {}
    return result;
}
"""


class BrowserDriver:
    """Wraps Playwright to drive a browser and capture errors."""

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
    ):
        self.headless = headless
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.console_errors: List[Dict] = []
        self.network_errors: List[Dict] = []
        # Full request/response log — every HTTP call this session has
        # made, regardless of status. Used by network_requests / network_request
        # tools so the agent can verify "did the right /api/foo get called
        # with the right payload" without manually scraping anything.
        self.network_log: List[Dict] = []
        # Request-mock routes registered by the agent. {pattern: {response_dict}}.
        self._mock_routes: Dict[str, Dict] = {}
        # Live route handlers, kept out of _mock_routes (which is surfaced to the
        # agent) so mocks can be suspended/restored around a clean re-load.
        self._mock_handlers: Dict[str, object] = {}
        # JS dialog handling — agent queues a response, the handler pops one
        # for each fired alert/confirm/prompt, falls back to auto-dismiss.
        self._dialog_queue: List[Dict] = []
        self._dialog_log: List[Dict] = []

    # -- lifecycle --

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(viewport=self.viewport)
        self._context.on("page", self._on_new_page)
        self._page = await self._context.new_page()
        self._attach_page_listeners(self._page)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

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
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("dialog", self._on_dialog)
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
        self._dialog_log.append({
            "type": dialog.type,
            "message": dialog.message,
            "responded_with": action if spec else "auto-dismiss",
            "text": text,
            "timestamp": datetime.now().isoformat(),
        })
        try:
            if action == "accept":
                await dialog.accept(text)
            else:
                await dialog.dismiss()
        except Exception:
            pass

    def queue_dialog_response(self, action: str, text: str = "") -> None:
        self._dialog_queue.append({"action": action, "text": text})

    def dialog_log_snapshot(self) -> List[Dict]:
        return list(self._dialog_log)

    def _setup_listeners(self):
        # Back-compat shim: old call site that wired listeners onto self._page.
        self._attach_page_listeners(self._page)

    def _on_console(self, msg):
        if msg.type in ("error", "warning"):
            self.console_errors.append({
                "type": msg.type,
                "text": msg.text,
                "url": self._page.url,
                "timestamp": datetime.now().isoformat(),
            })

    def _on_page_error(self, error):
        self.console_errors.append({
            "type": "exception",
            "text": str(error),
            "url": self._page.url,
            "timestamp": datetime.now().isoformat(),
        })

    def _on_request(self, request):
        try:
            entry = {
                "id": id(request),
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
                "post_data": request.post_data,
                "started_at": datetime.now().isoformat(),
                "page_url": self._page.url if self._page else "",
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
                    entry["response_headers"] = dict(response.headers)
                except Exception:
                    entry["response_headers"] = None
                try:
                    body = await response.body()
                    entry["response_size"] = len(body) if body else 0
                    # Keep the decoded body (capped/redacted) — the agent needs
                    # to read the payload behind a misleading toast (e.g. a 200
                    # whose body is {"error": ...}). Previously fetched only for
                    # its length and discarded.
                    entry["response_body"] = _capture_body(
                        body, entry.get("response_headers") or {})
                except Exception:
                    entry["response_size"] = None
                    entry["response_body"] = None
                entry["finished_at"] = datetime.now().isoformat()
        except Exception:
            pass

        if response.status >= 400:
            self.network_errors.append({
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
                "page_url": self._page.url,
                "timestamp": datetime.now().isoformat(),
            })

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
                f"(k) => {store}.removeItem(k)", key,
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
        from urllib.parse import urlparse
        applied = {"cookies": 0, "cookies_expected": 0, "local": 0, "local_expected": 0,
                   "session": 0, "session_expected": 0, "origin_ok": True}
        try:
            await self.cookies_clear()
        except Exception:
            pass
        cookies = capsule.get("cookies") or []
        applied["cookies_expected"] = len(cookies)
        if cookies:
            applied["cookies"] = await self.cookies_set(cookies)
        url = capsule.get("url") or ""
        if not url:
            return applied
        await self.goto(url)
        want = urlparse(url).netloc
        cur = urlparse(self._page.url).netloc if self._page else ""
        if want and cur and want != cur:
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
            try:
                title = await p.title()
            except Exception:
                title = ""
            out.append({
                "index": i,
                "url": p.url,
                "title": title,
                "active": p is self._page,
            })
        return out

    async def tabs_switch(self, index: int) -> bool:
        pages = self._live_pages()
        if index < 0 or index >= len(pages):
            return False
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
            "post_data": req.post_data,
        }

    # -- navigation --

    async def goto(self, url: str):
        if self._page is None:
            # All tabs were closed; reopen one so navigation recovers the
            # session instead of dereferencing a None page.
            self._page = await self._context.new_page()
            self._attach_page_listeners(self._page)
        # Returns the main-frame Response so callers can detect 4xx/5xx (a fresh
        # load that errored must not certify a symptom by its absence).
        return await self._page.goto(url, wait_until="networkidle", timeout=30_000)

    # -- state extraction --

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
                f"{ind['cls']}:{ind['text']}"
                for ind in content.get("cssIndicators", [])
            ],
            item_lists=content.get("itemLists", {}),
            links=content.get("links", []),
            images=content.get("images", []),
            meta_tags=content.get("metaTags", {}),
            headings=content.get("headings", []),
            accessibility_issues=content.get("a11yIssues", []),
            mixed_content=content.get("mixedContent", []),
            open_modals=content.get("openModals", []),
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


    async def _extract_elements(self, page=None) -> List[InteractiveElement]:
        raw = await (page or self._page).evaluate(_EXTRACT_ELEMENTS_JS)
        return [InteractiveElement(**el) for el in raw]

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
        selector = self._build_selector(elements[element_index])
        nth = sum(
            1 for other in elements[:element_index]
            if self._build_selector(other) == selector
        )
        return (page or self._page).locator(selector).nth(nth)

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
        try:
            storage = await self._context.storage_state()
        except Exception:
            storage = None
        ctx = await self._browser.new_context(viewport=self.viewport, storage_state=storage)
        # Count writes the replay re-executes against the backend, so the caller
        # can warn the agent that re-driving the journey re-performs side effects.
        writes: List[str] = []
        ctx.on("request", lambda req: writes.append(req.method)
               if (req.method or "").upper() in ("POST", "PUT", "PATCH", "DELETE") else None)
        page = await ctx.new_page()
        steps: List[Dict] = []
        diverged = False
        baseline = None
        try:
            if start_url:
                try:
                    resp = await page.goto(start_url, wait_until="networkidle", timeout=30_000)
                    if resp is not None and resp.status >= 400:
                        return {"steps": [{"act": f"goto {start_url}", "ok": False,
                                           "reason": f"HTTP {resp.status}"}],
                                "diverged": True, "baseline_state": None, "final_state": None}
                except Exception as e:
                    return {"steps": [{"act": f"goto {start_url}", "ok": False, "reason": str(e)[:80]}],
                            "diverged": True, "baseline_state": None, "final_state": None}
            baseline = await self.get_state(page)
            for act in actions:
                tool = act.get("tool")
                desc = act.get("description") or ""
                val = act.get("value")
                if tool == "navigate":
                    try:
                        resp = await page.goto(val, wait_until="networkidle", timeout=30_000)
                        if resp is not None and resp.status >= 400:
                            steps.append({"act": f"navigate {val}", "ok": False, "reason": f"HTTP {resp.status}"})
                            diverged = True
                            break
                        steps.append({"act": f"navigate {val}", "ok": True})
                    except Exception as e:
                        steps.append({"act": f"navigate {val}", "ok": False, "reason": str(e)[:80]})
                        diverged = True
                        break
                    continue
                kind = {"type_into": "input", "select_into": "select"}.get(tool)
                elements = await self._extract_elements(page)
                r = resolve_element(desc, elements, kind_filter=kind, strict_kind=bool(kind))
                if r.reason != "unique" or r.found is None:
                    steps.append({"act": f"{tool} {desc!r}", "ok": False, "reason": f"re-resolve {r.reason}"})
                    diverged = True
                    break
                try:
                    loc = self._locator(elements.index(r.found), elements, page=page)
                    if tool == "click_what":
                        await loc.click(timeout=5_000)
                        await page.wait_for_load_state("networkidle", timeout=10_000)
                    elif tool == "type_into":
                        await loc.fill(val or "", timeout=5_000)
                    elif tool == "select_into":
                        await loc.select_option(val or "", timeout=5_000)
                    else:
                        steps.append({"act": f"{tool} {desc!r}", "ok": False, "reason": "unknown tool"})
                        diverged = True
                        break
                    steps.append({"act": f"{tool} {desc!r}", "ok": True})
                except Exception as e:
                    steps.append({"act": f"{tool} {desc!r}", "ok": False, "reason": str(e)[:80]})
                    diverged = True
                    break
            final = None if diverged else await self.get_state(page)
            return {"steps": steps, "diverged": diverged, "writes": len(writes),
                    "baseline_state": baseline, "final_state": final}
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    async def click(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        try:
            await self._locator(element_index, elements).click(timeout=5_000)
            await self._page.wait_for_load_state("networkidle", timeout=10_000)
            return True
        except Exception:
            return False

    async def type_text(
        self, element_index: int, text: str, elements: List[InteractiveElement]
    ) -> bool:
        try:
            await self._locator(element_index, elements).fill(text, timeout=5_000)
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

    async def hover(
        self, element_index: int, elements: List[InteractiveElement]
    ) -> bool:
        try:
            await self._locator(element_index, elements).hover(timeout=5_000)
            return True
        except Exception:
            return False

    async def right_click(
        self, element_index: int, elements: List[InteractiveElement]
    ) -> bool:
        try:
            await self._locator(element_index, elements).click(button="right", timeout=5_000)
            return True
        except Exception:
            return False

    async def drag(
        self, source_index: int, target_index: int,
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
        self, element_index: int, paths: List[str],
        elements: List[InteractiveElement],
    ) -> bool:
        try:
            await self._locator(element_index, elements).set_input_files(paths, timeout=5_000)
            return True
        except Exception:
            return False

    async def go_back(self) -> bool:
        try:
            await self._page.go_back(wait_until="networkidle", timeout=10_000)
            return True
        except Exception:
            return False

    async def scroll_down(self):
        if self._page is None:
            return
        await self._page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(0.5)

    async def screenshot(self, path: str, full_page: bool = False) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=path, full_page=full_page)
        return path

    async def element_screenshot(self, selector: str, path: str) -> Optional[str]:
        """Capture just the bounds of one element (Playwright crops natively)."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
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
                resp = await self._context.request.head(href, timeout=5000)
                # 405 = server doesn't support HEAD, retry with GET
                if resp.status == 405:
                    resp = await self._context.request.get(href, timeout=5000)
                # 403 from context.request often means anti-bot, not a real dead link
                # Mark as ok since the page loaded fine in the browser
                is_ok = resp.ok or resp.status == 403
                results.append({"href": href, "status": resp.status, "ok": is_ok})
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
                return f'{el.tag} >> text={snippet}'
            return f'{el.tag}:has-text("{text_escaped}")'
        # Last resort: tag + type
        if el.type and el.tag == "input":
            return f'{el.tag}[type="{el.type}"]'
        return el.tag
