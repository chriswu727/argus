"""Browser-integration tests for shadow-DOM piercing in observe.

Unlike the other test modules (pure-Python, no browser), these launch a
real Chromium and run the actual extraction JS + selector builder, because
shadow-DOM behaviour only exists at the browser boundary. Skipped cleanly
when the Chromium binary isn't installed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from playwright.async_api import async_playwright

from argus.browser import _EXTRACT_ELEMENTS_JS, BrowserDriver
from argus.models import InteractiveElement


_PAGE = """
<button id="light-btn">Light Button</button>
<div id="host"></div>
<script>
  const r = document.getElementById('host').attachShadow({mode:'open'});
  r.innerHTML =
      '<button id="shadow-id-btn" onclick="this.dataset.hit=1">Shadow ById</button>'
    + '<button class="txt" onclick="this.dataset.hit=1">Save Draft</button>'
    + '<button aria-label="Close panel" onclick="this.dataset.hit=1">x</button>'
    + '<div id="nested"></div>';
  const n = r.getElementById('nested').attachShadow({mode:'open'});
  n.innerHTML = '<button id="deep-btn" onclick="this.dataset.hit=1">Deep</button>';
</script>
"""


@asynccontextmanager
async def _page_with(content: str):
    """Yield a live (page, elements) pair. Everything must run inside the
    `async with`: leaving it tears down the Playwright connection."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:  # binary not installed in this env
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(content)
            raw = await page.evaluate(_EXTRACT_ELEMENTS_JS)
            els = [InteractiveElement(**r) for r in raw]
            yield page, els
        finally:
            await browser.close()


async def test_observe_pierces_open_shadow_dom():
    async with _page_with(_PAGE) as (_page, els):
        by_text = {e.text: e for e in els}
        # Light element seen, not flagged shadow.
        assert by_text["Light Button"].shadow is False
        # All four shadow elements surfaced, including the 2-levels-deep one.
        for label in ("Shadow ById", "Save Draft", "x", "Deep"):
            assert label in by_text, f"{label!r} missing from observe"
            assert by_text[label].shadow is True


async def test_shadow_elements_are_actionable():
    """Every surfaced shadow element must produce a selector that clicks it —
    no 'visible but unreachable' half-feature. Text-only is the hard case:
    :has-text doesn't pierce shadow, so _build_selector must switch forms."""
    async with _page_with(_PAGE) as (page, els):
        async def clicked(el: InteractiveElement) -> bool:
            sel = BrowserDriver._build_selector(el)
            await page.click(sel, timeout=2000)
            return await page.evaluate(
                """(t)=>{function f(root){for(const el of root.querySelectorAll('*')){
                    if(el.textContent===t && el.dataset.hit) return true;
                    if(el.shadowRoot && f(el.shadowRoot)) return true;} return false;}
                    return f(document);}""",
                el.text,
            )

        by_text = {e.text: e for e in els}
        for label in ("Shadow ById", "Save Draft", "x", "Deep"):
            assert await clicked(by_text[label]), f"{label!r} built an unclickable selector"
