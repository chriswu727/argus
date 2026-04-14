from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from typing import Dict, List, Optional, Tuple

from .models import InteractiveElement, PageState

# JS snippet to extract visible interactive elements from the page.
_EXTRACT_ELEMENTS_JS = """
() => {
    const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick], [tabindex]:not([tabindex="-1"])';
    const els = document.querySelectorAll(sel);
    return Array.from(els).map((el, i) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (rect.width === 0 || rect.height === 0 || style.display === 'none' || style.visibility === 'hidden') return null;
        return {
            index: i,
            tag: el.tagName.toLowerCase(),
            type: el.type || null,
            text: (el.textContent || '').trim().slice(0, 100) || null,
            placeholder: el.placeholder || null,
            href: el.href || null,
            value: el.value || null,
            disabled: el.disabled || false,
            role: el.getAttribute('role') || null,
            aria_label: el.getAttribute('aria-label') || null,
            name: el.name || null,
            id: el.id || null,
            parent_context: (el.closest('li, tr, .card, .list-item, [class*="item"], [class*="row"]') || {}).textContent?.trim()?.slice(0, 200) || null,
        };
    }).filter(Boolean);
}
"""

# JS snippet to extract full page content for smart detection.
_EXTRACT_PAGE_CONTENT_JS = """
() => {
    const result = { pageText: '', toasts: [], counts: {}, cssIndicators: [], itemLists: {},
                     links: [], images: [], metaTags: {}, headings: [], a11yIssues: [], mixedContent: [] };

    // 1. Full visible text — simple and robust
    try {
        result.pageText = (document.body.innerText || '').slice(0, 5000);
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

    # -- lifecycle --

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(viewport=self.viewport)
        self._page = await self._context.new_page()
        self._setup_listeners()

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # -- listeners --

    def _setup_listeners(self):
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)
        self._page.on("response", self._on_response)

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

    async def _on_response(self, response):
        if response.status >= 400:
            self.network_errors.append({
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
                "page_url": self._page.url,
                "timestamp": datetime.now().isoformat(),
            })

    # -- navigation --

    async def goto(self, url: str):
        await self._page.goto(url, wait_until="networkidle", timeout=30_000)

    # -- state extraction --

    async def get_state(self) -> PageState:
        elements = await self._extract_elements()
        content = await self._extract_page_content()
        return PageState(
            url=self._page.url,
            title=await self._page.title(),
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
        )

    async def refresh_and_get_state(self) -> PageState:
        """Reload the current page and return fresh state for verification."""
        await self._page.reload(wait_until="networkidle", timeout=15_000)
        return await self.get_state()

    async def _extract_elements(self) -> List[InteractiveElement]:
        raw = await self._page.evaluate(_EXTRACT_ELEMENTS_JS)
        return [InteractiveElement(**el) for el in raw]

    async def _extract_page_content(self) -> Dict:
        try:
            return await self._page.evaluate(_EXTRACT_PAGE_CONTENT_JS)
        except Exception:
            return {}

    # -- actions --

    async def click(self, element_index: int, elements: List[InteractiveElement]) -> bool:
        el = elements[element_index]
        selector = self._build_selector(el)
        try:
            await self._page.click(selector, timeout=5_000)
            await self._page.wait_for_load_state("networkidle", timeout=10_000)
            return True
        except Exception:
            return False

    async def type_text(
        self, element_index: int, text: str, elements: List[InteractiveElement]
    ) -> bool:
        el = elements[element_index]
        selector = self._build_selector(el)
        try:
            await self._page.fill(selector, text, timeout=5_000)
            return True
        except Exception:
            return False

    async def select_option(
        self, element_index: int, value: str, elements: List[InteractiveElement]
    ) -> bool:
        el = elements[element_index]
        selector = self._build_selector(el)
        try:
            await self._page.select_option(selector, value, timeout=5_000)
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
        await self._page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(0.5)

    async def screenshot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=path, full_page=False)
        return path

    # -- element matching helpers --

    @staticmethod
    def find_element_by_field(
        field_key: str, elements: List[InteractiveElement]
    ) -> Optional[int]:
        """Find an input element matching a field key by name, id, placeholder, or aria-label."""
        key_lower = field_key.lower()
        # Priority: name > id > placeholder > aria-label
        for el in elements:
            if el.tag not in ("input", "textarea", "select"):
                continue
            if el.type in ("submit", "button", "hidden"):
                continue
            if el.name and el.name.lower() == key_lower:
                return el.index
        for el in elements:
            if el.tag not in ("input", "textarea", "select"):
                continue
            if el.type in ("submit", "button", "hidden"):
                continue
            if el.id and el.id.lower() == key_lower:
                return el.index
        for el in elements:
            if el.tag not in ("input", "textarea", "select"):
                continue
            if el.type in ("submit", "button", "hidden"):
                continue
            if el.placeholder and key_lower in el.placeholder.lower():
                return el.index
        for el in elements:
            if el.tag not in ("input", "textarea", "select"):
                continue
            if el.type in ("submit", "button", "hidden"):
                continue
            if el.aria_label and key_lower in el.aria_label.lower():
                return el.index
        return None

    @staticmethod
    def find_button_near_item(
        item_text: str, keywords: List[str], elements: List[InteractiveElement]
    ) -> Optional[int]:
        """Find a button matching keywords whose parent_context contains item_text."""
        item_lower = item_text.lower()
        for el in elements:
            if el.tag not in ("button", "a") and el.role not in ("button",):
                continue
            el_text = (el.text or "").lower()
            el_label = (el.aria_label or "").lower()
            has_keyword = any(kw in el_text or kw in el_label for kw in keywords)
            if not has_keyword:
                continue
            if el.parent_context and item_lower in el.parent_context.lower():
                return el.index
        # Fallback: match keyword without parent context (first match)
        for el in elements:
            if el.tag not in ("button", "a") and el.role not in ("button",):
                continue
            el_text = (el.text or "").lower()
            if any(kw in el_text for kw in keywords):
                return el.index
        return None

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
            return f"#{el.id}"
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
            text_escaped = el.text[:50].replace('"', '\\"')
            return f'{el.tag}:has-text("{text_escaped}")'
        # Last resort: tag + type
        if el.type and el.tag == "input":
            return f'{el.tag}[type="{el.type}"]'
        return el.tag
