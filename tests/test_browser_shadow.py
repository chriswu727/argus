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


def test_build_selector_handles_multiline_and_special_chars():
    """A card link's textContent is multi-line and full of CSS-hostile chars
    ("[no image]\\n Name\\n $9.99"). The built selector must not contain a raw
    newline (CSS BADSTRING) — regression for a real click failure found while
    dogfooding the shop fixture."""
    from argus.browser import BrowserDriver
    el = InteractiveElement(
        index=0, tag="a",
        text="[no image]\n            Wireless Headphones\n            $89.99-50%$89.99",
    )
    sel = BrowserDriver._build_selector(el)
    assert "\n" not in sel
    assert sel.startswith("a:has-text(")
    # the human-meaningful product name survives into the substring
    assert "Wireless Headphones" in sel


def test_build_selector_shadow_long_label_uses_substring_form():
    """A shadow label >50 chars can't use exact text="..." (a truncated snippet
    matches zero elements); it must fall back to the unquoted substring form."""
    from argus.browser import BrowserDriver
    long_label = ("This is a very long shadow web-component menu item label that "
                  "clearly exceeds fifty characters")
    sel = BrowserDriver._build_selector(
        InteractiveElement(index=0, tag="button", text=long_label, shadow=True))
    assert sel.startswith("button >> text=")
    assert 'text="' not in sel  # NOT the exact-quoted form
    # a short shadow label keeps the precise exact-quoted form
    assert BrowserDriver._build_selector(
        InteractiveElement(index=0, tag="button", text="OK", shadow=True)
    ) == 'button >> text="OK"'


def test_build_selector_escapes_css_special_ids():
    """Modern-framework ids carry ':' (React useId, Radix, MUI) or '.', which
    are CSS-special. A raw "#:r3:" is a parse error and "#ok.x" mis-targets, so
    the id branch must emit the quoted attribute form, not "#id"."""
    from argus.browser import BrowserDriver
    assert BrowserDriver._build_selector(
        InteractiveElement(index=0, tag="button", id=":r3:")) == '[id=":r3:"]'
    assert BrowserDriver._build_selector(
        InteractiveElement(index=0, tag="div", id="ok.x")) == '[id="ok.x"]'
    # a quote inside the id is escaped, not left to break the selector string
    assert BrowserDriver._build_selector(
        InteractiveElement(index=0, tag="div", id='a"b')) == '[id="a\\"b"]'


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
