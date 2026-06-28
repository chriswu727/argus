"""Replay-receipt minimization: narrow a confirmed write-free reproduction to
its minimal sufficient steps; skip (don't re-run) a journey that does writes."""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

import argus.mcp_server as m


def _serve(handler):
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def _call(tool, **kw):
    return await (getattr(tool, "fn", tool))(**kw)


async def _start(url):
    try:
        await _call(m.start_session, url=url)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


class _ReadOnly(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/b"):
            body = "<h1>Step 2</h1><p>BUG-XYZ here</p>"
        else:
            body = '<h1>Step 1</h1><button id=n>Noop</button> <a href="/b">Go</a>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())


async def test_minimize_drops_unnecessary_step():
    srv, base = _serve(_ReadOnly)
    await _start(base + "/a")
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Noop")  # unnecessary
        await _call(m.click_what, description="Go")     # the one that matters
        await _call(m.record_bug, title="multi", severity="medium",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "minimize": True,
                            "expect": "present", "target_text": "BUG-XYZ"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["reproduced"] is True
        assert r["minimal_count"] == 1            # 2 steps -> 1 suffices
        assert any("Go" in s for s in r["minimal_steps"])
    finally:
        await _call(m.end_session)
        srv.shutdown()


class _Writes(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = ("<button id=n>Noop</button><button id=s>Save</button>"
                "<div id=t role=alert style='display:none'>Saved!</div>"
                "<script>document.getElementById('s').onclick=()=>{"
                "fetch('/api/save',{method:'POST'});"
                "document.getElementById('t').style.display='block';};</script>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())

    def do_POST(self):
        self.send_response(500)
        self.end_headers()


async def test_minimize_skipped_for_write_journey():
    srv, base = _serve(_Writes)
    await _start(base + "/")
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Noop")
        await _call(m.click_what, description="Save")  # fires a POST (write)
        await _call(m.record_bug, title="write", severity="medium",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "minimize": True,
                            "expect": "present", "target_text": "Saved"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["reproduced"] is True
        assert r.get("writes_replayed", 0) >= 1
        assert "minimize_skipped" in r       # not minimized — would repeat writes
        assert "minimal_count" not in r
    finally:
        await _call(m.end_session)
        srv.shutdown()
