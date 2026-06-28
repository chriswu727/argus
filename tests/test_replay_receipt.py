"""Live tests for the cold-replay receipt: reproduce a multi-step journey from a
fresh page, with a three-state verdict (reproduced / not / path-diverged)."""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

import argus.mcp_server as m


class _H(http.server.BaseHTTPRequestHandler):
    serve_go = True  # when False, /a no longer offers the "Go" link

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/b"):
            body = "<h1>Step 2</h1><p>BUG-XYZ visible here</p>"
        else:
            link = '<a href="/b">Go</a>' if _H.serve_go else "<p>no link</p>"
            body = f"<h1>Step 1</h1>{link}"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())


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
        await _call(m.start_session, url=url)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


async def test_replay_reproduces_multistep_journey():
    _H.serve_go = True
    srv, base = _serve()
    await _start(base + "/a")
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Go")  # navigates to /b
        await _call(m.record_bug, title="multistep", severity="medium",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "expect": "present", "target_text": "BUG-XYZ"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["mode"] == "replay"
        assert r["reproduced"] is True
        assert r["steps"] == 1
    finally:
        await _call(m.end_session)
        srv.shutdown()


async def test_replay_path_divergence_is_inconclusive():
    _H.serve_go = True
    srv, base = _serve()
    await _start(base + "/a")
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Go")
        _H.serve_go = False  # the "Go" link is gone on a fresh load now
        await _call(m.record_bug, title="diverged", severity="low",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "expect": "present", "target_text": "BUG-XYZ"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["mode"] == "replay"
        assert r["reproduced"] is None  # never certified
        assert r["diverged"] is True
    finally:
        await _call(m.end_session)
        srv.shutdown()


async def test_replay_preexisting_symptom_is_inconclusive():
    # "Step" appears on the START page (Step 1) already, so the journey can't be
    # credited with causing it -> INCONCLUSIVE, never a false certify (the flip
    # requirement; guards pre-existing text / localStorage residue / value echo).
    _H.serve_go = True
    srv, base = _serve()
    await _start(base + "/a")
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Go")
        await _call(m.record_bug, title="preexist", severity="low",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "expect": "present", "target_text": "Step"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["reproduced"] is None
        assert r.get("symptom_before") is True
    finally:
        await _call(m.end_session)
        srv.shutdown()


async def test_replay_without_steps_is_not_attempted():
    srv, base = _serve()
    await _start(base + "/a")
    try:
        # no actions recorded -> replay can't run, must not falsely certify
        await _call(m.record_bug, title="nosteps", severity="low",
                    evidence={"screenshot": "skip"},
                    verify={"replay": True, "expect": "present", "target_text": "BUG-XYZ"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["attempted"] is False
    finally:
        await _call(m.end_session)
        srv.shutdown()
