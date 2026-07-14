"""Persistent journal + receipt-driven regression: a finding recorded and
journaled in one session is re-tested against the current build in the next —
STILL-PRESENT vs NO-LONGER-REPRODUCES — by re-running its clean-load receipt."""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

import argus.mcp_server as m
from argus.models import Bug, BugType, Severity


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


async def _call(tool, **kw):
    return await (getattr(tool, "fn", tool))(**kw)


async def _start(url):
    try:
        return await _call(m.start_session, url=url)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


async def test_journal_and_regression_roundtrip():
    _H.bug_present = True
    srv, base = _serve()
    try:
        # Session 1: confirm + record the bug, then end (journals it).
        await _start(base + "/")
        rb = await _call(m.record_bug, title="ghost bug", severity="high",
                         evidence={"screenshot": "skip"},
                         verify={"expect": "present", "target_text": "GHOST-BUG", "at_url": "/"})
        assert "CONFIRMED" in rb
        await _call(m.end_session)

        # Session 2: start_session hints at the journaled finding; regression
        # re-tests it as STILL-PRESENT, then NO-LONGER-REPRODUCES once "fixed".
        start_msg = await _start(base + "/")
        assert "regression_check" in start_msg
        assert "STILL-PRESENT" in await _call(m.regression_check)
        _H.bug_present = False  # ship the fix
        assert "NO-LONGER-REPRODUCES" in await _call(m.regression_check)
    finally:
        try:
            await _call(m.end_session)
        except Exception:
            pass
        srv.shutdown()


def test_status_receipt_is_journaled_for_regression(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path))
    session = m.Session()
    session.url = "https://example.test/"
    session.bugs = [Bug(
        type=BugType.BROKEN_LINK,
        severity=Severity.MEDIUM,
        title="Missing route",
        description="Returns 404",
        url="https://example.test/missing",
        steps_to_reproduce=[],
        reproduction_receipt={
            "attempted": True,
            "reproduced": True,
            "expect_status": 404,
            "at_url": "https://example.test/missing",
        },
    )]

    m._write_journal(session)

    entries = m._journal_entries("example.test")
    assert entries[0]["verify"] == {
        "at_url": "https://example.test/missing",
        "expect_status": 404,
    }
