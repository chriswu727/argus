"""A new user has no Chromium yet; the first session fetches it."""

from __future__ import annotations

import http.server
import threading

import pytest

import argus.application.lifecycle as lifecycle
from argus.application.lifecycle import start_session
from argus.application.manager import SessionState
from argus.domain.contracts import ErrorCode

MISSING = "BrowserType.launch: Executable doesn't exist at /cache/chromium/headless-shell"


@pytest.fixture
def page():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Ready</h1><button>Go</button>")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _missing_once(runtime, monkeypatch, error=MISSING):
    """Make the first browser launch fail as it does without Chromium."""
    factory = runtime.browser_factory
    state = {"fail": True}

    def build(**kwargs):
        browser = factory(**kwargs)
        start = browser.start

        async def flaky_start():
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError(error)
            await start()

        browser.start = flaky_start
        return browser

    monkeypatch.setattr(runtime, "browser_factory", build)


def _installer(monkeypatch, outcome):
    calls = []

    async def install(headless, wait_seconds):
        calls.append((headless, wait_seconds))
        return outcome

    monkeypatch.setattr(lifecycle, "install_chromium", install)
    return calls


async def test_missing_browser_is_downloaded_and_the_session_starts(
    isolated_runtime, monkeypatch, page
):
    _missing_once(isolated_runtime, monkeypatch)
    calls = _installer(monkeypatch, "installed")
    try:
        result = await isolated_runtime.execute("start_session", start_session, page)
        assert result.error is None, result.value
        assert len(calls) == 1 and calls[0][0] is True and 0 < calls[0][1] <= 90
        assert "Chromium was downloaded for first use" in str(result.value)
        assert isolated_runtime.session.active
    finally:
        await isolated_runtime.shutdown()


async def test_slow_download_asks_for_a_retry_and_keeps_going(isolated_runtime, monkeypatch, page):
    _missing_once(isolated_runtime, monkeypatch)
    _installer(monkeypatch, "downloading")
    result = await isolated_runtime.execute("start_session", start_session, page)
    assert result.error is ErrorCode.BACKEND
    assert "call start_session again in a minute" in str(result.value)
    assert isolated_runtime.state is SessionState.FAILED


async def test_failed_download_reports_the_manual_command(isolated_runtime, monkeypatch, page):
    _missing_once(isolated_runtime, monkeypatch)
    _installer(monkeypatch, "failed")
    result = await isolated_runtime.execute("start_session", start_session, page)
    assert result.error is ErrorCode.BACKEND
    assert "playwright install chromium" in str(result.value)


async def test_auto_install_can_be_turned_off(isolated_runtime, monkeypatch, page):
    monkeypatch.setenv("ARGUS_AUTO_INSTALL_BROWSER", "0")
    _missing_once(isolated_runtime, monkeypatch)
    calls = _installer(monkeypatch, "installed")
    result = await isolated_runtime.execute("start_session", start_session, page)
    assert result.error is ErrorCode.BACKEND and calls == []
    assert "playwright install chromium" in str(result.value)


async def test_other_launch_failures_do_not_trigger_a_download(isolated_runtime, monkeypatch, page):
    _missing_once(isolated_runtime, monkeypatch, error="Host system is missing dependencies")
    calls = _installer(monkeypatch, "installed")
    result = await isolated_runtime.execute("start_session", start_session, page)
    assert result.error is ErrorCode.BACKEND and calls == []
    assert "install-deps" in str(result.value)
