"""Zero-LLM `argus-regression` CLI: re-tests journaled findings against the
current build and exits non-zero on STILL-PRESENT (CI gate)."""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
from urllib.parse import urlparse

import pytest

import argus.mcp_server as m
from argus.cli import _run_regression


class _H(http.server.BaseHTTPRequestHandler):
    bug_present = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = "GHOST-BUG" if _H.bug_present else "all clean"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body><p>{body}</p></body></html>".encode())


def _serve():
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def test_regression_cli_exit_codes(tmp_path, monkeypatch):
    _H.bug_present = True
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path))
    srv, base = _serve()
    origin = urlparse(base + "/").netloc
    jpath = m._journal_path(origin)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json.dumps([{
        "fingerprint": "f", "title": "ghost", "severity": "high", "type": "ux_issue",
        "verify": {"expect": "present", "target_text": "GHOST-BUG", "at_url": "/"}}]))
    try:
        code = await _run_regression(base + "/", str(tmp_path), headless=True)
        if code == 2:
            pytest.skip("Chromium unavailable")
        assert code == 1  # bug STILL-PRESENT -> non-zero (CI fails)

        _H.bug_present = False  # ship the fix
        assert await _run_regression(base + "/", str(tmp_path), headless=True) == 0
    finally:
        srv.shutdown()


async def test_regression_cli_no_journal_is_clean_exit(tmp_path):
    # An origin with no journal exits 0 (nothing to gate on), never errors.
    assert await _run_regression("http://no-such-host.invalid/", str(tmp_path), headless=True) == 0
