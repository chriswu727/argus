"""Session-lifecycle & mode-guard regression tests.

Covers a cluster of crash/state-drift bugs:
  - mode switch teardown crashed (None.stop()) going screen -> web (F18);
  - web-only tools NPE'd in screen mode instead of a friendly error (F17);
  - get_state dereferenced a None page after all tabs closed (F16);
  - tabs_switch left the resolver pool pointing at the previous tab (F16).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import argus.mcp_server as m
from argus.browser import BrowserDriver


def _file_url(label: str) -> str:
    html = f"<html><body><h1>{label}</h1><button>{label} Btn</button></body></html>"
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    return Path(f.name).as_uri()


class _FakeScreen:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


# ── F18: teardown handles a screen session with no browser ───────────

async def test_teardown_stops_screen_without_browser():
    orig = m._session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FakeScreen()  # browser stays None
        m._session = sess
        await m._teardown_active_session()  # must not raise None.stop()
        assert sess.screen.stopped is True
    finally:
        m._session = orig


# ── F17: web-only tools reject a screen session cleanly ──────────────

async def test_web_tools_reject_screen_session():
    orig = m._session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FakeScreen()
        m._session = sess
        for tool, kwargs in (
            (m.navigate, {"url": "http://example.test"}),
            (m.go_back, {}),
            (m.scroll_down, {}),
        ):
            fn = getattr(tool, "fn", tool)
            out = await fn(**kwargs)
            assert "web-mode only" in out
    finally:
        m._session = orig


# ── F16: get_state guards a closed-out page ──────────────────────────

async def test_get_state_raises_when_no_open_page():
    drv = BrowserDriver()  # never started -> _page is None
    with pytest.raises(RuntimeError, match="No open page"):
        await drv.get_state()


async def test_tab_recovery_after_closing_all_tabs():
    drv = BrowserDriver(headless=True)
    try:
        await drv.start()
        await drv.goto(_file_url("One"))
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")
    try:
        await drv._context.new_page()  # open a second tab
        assert len(await drv.tabs_list()) == 2
        await drv.tabs_switch(0)
        assert await drv.get_state() is not None  # switch must not crash

        while drv._live_pages():  # close every tab
            await drv.tabs_close(0)
        assert drv._page is None
        with pytest.raises(RuntimeError):
            await drv.get_state()

        await drv.goto(_file_url("Recovered"))  # goto reopens a page
        assert drv._page is not None
        assert any("Recovered" in (e.text or "") for e in (await drv.get_state()).elements)
    finally:
        await drv.stop()


# ── F16: tabs_switch refreshes the resolver pool to the new tab ───────

async def test_tabs_switch_refreshes_resolver_pool():
    orig = m._session
    start = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    end = m.end_session.fn if hasattr(m.end_session, "fn") else m.end_session
    switch = m.tabs_switch.fn if hasattr(m.tabs_switch, "fn") else m.tabs_switch
    try:
        try:
            await start(_file_url("Alpha"))
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        s = m._session
        p2 = await s.browser._context.new_page()
        await p2.goto(_file_url("Beta"))

        out = await switch(1)
        assert "Switched to tab 1" in out
        # The pool the resolver uses must now reflect tab 1 (Beta), not Alpha.
        assert any("Beta" in (e.text or "") for e in s._last_elements)
        assert not any("Alpha" in (e.text or "") for e in s._last_elements)
    finally:
        try:
            await end()
        except Exception:
            pass
        m._session = orig
