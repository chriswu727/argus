"""Live-browser regression tests for action targeting.

Two silent-wrong-action bugs lived between resolution and the actual click:
  - duplicate controls (N identical "Delete") all built the same selector, so
    page.click hit the first DOM match — defeating the resolver's row/ordinal
    pick (F3);
  - the agent's own network mocks stayed live during the reproduction re-load,
    so an injected symptom could certify itself "reproduced" (F2).

These exercise the real Playwright path and skip when Chromium is absent.
"""
from __future__ import annotations

import functools
import http.server
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from argus.browser import BrowserDriver
from argus.resolver import resolve_element


async def _driver_on_file(html: str) -> BrowserDriver:
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    drv = BrowserDriver(headless=True)
    try:
        await drv.start()
        await drv.goto(Path(f.name).as_uri())
    except Exception as exc:  # Chromium not installed in this env
        pytest.skip(f"Chromium/browser unavailable: {exc}")
    return drv


_DUP_ROWS = """<html><body><ul id="list">
  <li>Buy groceries <button>Delete</button></li>
  <li>Pay rent <button>Delete</button></li>
  <li>Walk dog <button>Delete</button></li>
</ul><script>
  document.querySelectorAll('#list button').forEach(
    b => b.onclick = () => b.closest('li').remove());
</script></body></html>"""


async def test_ordinal_click_hits_resolved_duplicate_not_the_first():
    drv = await _driver_on_file(_DUP_ROWS)
    try:
        els = (await drv.get_state()).elements
        r = resolve_element("Delete #2", els)
        assert r.reason == "unique"
        # All three Deletes collapse to one selector — so a plain page.click
        # would hit the first; this is the case _locator's nth must handle.
        selectors = {drv._build_selector(e) for e in els if e.text == "Delete"}
        assert len(selectors) == 1

        await drv.click(els.index(r.found), els)

        remaining = await drv._page.eval_on_selector_all(
            "#list li",
            "ns => ns.map(n => n.textContent.trim().split(' Delete')[0])",
        )
        # The SECOND row ("Pay rent") must be gone — not the first.
        assert remaining == ["Buy groceries", "Walk dog"]
    finally:
        await drv.stop()


_FETCH_PAGE = """<html><body><div id="out">start</div><script>
  fetch('/api/data').then(r => r.json())
    .then(j => document.getElementById('out').textContent = j.v)
    .catch(() => document.getElementById('out').textContent = 'NOFETCH');
</script></body></html>"""


async def test_suspended_mock_does_not_fire_on_clean_reload():
    serve_dir = tempfile.mkdtemp()
    (Path(serve_dir) / "page.html").write_text(_FETCH_PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=serve_dir)

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

        def log_message(self, *a):  # silence access log
            pass

    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/page.html"

    drv = BrowserDriver(headless=True)
    try:
        try:
            await drv.start()
        except Exception as exc:
            pytest.skip(f"Chromium/browser unavailable: {exc}")

        await drv.add_route("**/api/data", status=200, body='{"v":"MOCKED"}')
        await drv.goto(url)
        assert await drv._page.text_content("#out") == "MOCKED"

        suspended = await drv.suspend_mocks()
        assert suspended == ["**/api/data"]
        await drv.goto(url)
        # /api/data 404s for real now — the injected symptom must NOT reappear.
        assert await drv._page.text_content("#out") == "NOFETCH"

        await drv.restore_mocks(suspended)
        await drv.goto(url)
        assert await drv._page.text_content("#out") == "MOCKED"
    finally:
        await drv.stop()
        srv.shutdown()
