"""Session-lifecycle & mode-guard regression tests.

Covers a cluster of crash/state-drift bugs:
  - mode switch teardown crashed (None.stop()) going screen -> web (F18);
  - web-only tools NPE'd in screen mode instead of a friendly error (F17);
  - get_state dereferenced a None page after all tabs closed (F16);
  - tabs_switch left the resolver pool pointing at the previous tab (F16).
"""

from __future__ import annotations

import asyncio
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


class _FailingScreen:
    async def stop(self):
        raise RuntimeError("cannot stop")


# ── F18: teardown handles a screen session with no browser ───────────


async def test_teardown_stops_screen_without_browser():
    orig = m.runtime.session
    try:
        sess = m.Session()
        sess.mode = "screen"
        screen = sess.screen = _FakeScreen()  # browser stays None
        m.runtime.session = sess
        await m._teardown_active_session()  # must not raise None.stop()
        assert screen.stopped is True
        assert sess.screen is None
    finally:
        m.runtime.session = orig


async def test_teardown_reports_backend_failure():
    orig = m.runtime.session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FailingScreen()
        m.runtime.session = sess

        errors = await m._teardown_active_session()

        assert errors and "cannot stop" in errors[0]
    finally:
        m.runtime.session = orig


async def test_session_lifecycle_transitions_do_not_overlap():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transition():
        entered.set()
        await release.wait()
        return "done"

    first = asyncio.create_task(m.runtime.execute("first", transition))
    await entered.wait()
    second = asyncio.create_task(m.runtime.execute("second", transition))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()

    assert (await first).value == "done"
    assert (await second).value == "done"


async def test_start_refuses_to_leak_backend_after_teardown_failure():
    orig = m.runtime.session
    start = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FailingScreen()
        m.runtime.session = sess

        result = await start("https://example.test")

        assert "could not close the active session safely" in result
        assert m.runtime.session is sess
    finally:
        m.runtime.session = orig


async def test_end_session_surfaces_report_failure_and_resets(monkeypatch):
    orig = m.runtime.session
    end = m.end_session.fn if hasattr(m.end_session, "fn") else m.end_session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FakeScreen()
        sess.start_time = asyncio.get_running_loop().time()
        m.runtime.session = sess

        def fail_report(*args, **kwargs):
            raise RuntimeError("disk unavailable")

        monkeypatch.setattr(m.Reporter, "generate", fail_report)
        result = await end()

        assert "report could not be saved" in result
        assert "disk unavailable" in result
        assert not m.runtime.session.active
    finally:
        m.runtime.session = orig


# ── F17: web-only tools reject a screen session cleanly ──────────────


async def test_web_tools_reject_screen_session():
    orig = m.runtime.session
    try:
        sess = m.Session()
        sess.mode = "screen"
        sess.screen = _FakeScreen()
        m.runtime.session = sess
        for tool, kwargs in (
            (m.navigate, {"url": "http://example.test"}),
            (m.go_back, {}),
            (m.scroll_down, {}),
        ):
            fn = getattr(tool, "fn", tool)
            out = await fn(**kwargs)
            assert "web-mode only" in out
    finally:
        m.runtime.session = orig


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
    orig = m.runtime.session
    start = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    end = m.end_session.fn if hasattr(m.end_session, "fn") else m.end_session
    switch = m.tabs_switch.fn if hasattr(m.tabs_switch, "fn") else m.tabs_switch
    try:
        try:
            await start(_file_url("Alpha"))
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        s = m.runtime.session
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
        m.runtime.session = orig
