"""Cross-stack action truth: reconcile the UI's success claim against the wire.

The seam pure-FE tests (never see persistence) and pure-BE tests (never see the
UI lie) both miss. Flags are observations, raised only for correlation-SAFE
contradictions; heuristic request attribution is never an auto-verdict.
"""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

import argus.mcp_server as m
from argus.mcp_server import _reconcile_action
from tests.conftest import make_page_state


def _req(method, status, url="/api/x"):
    return {"method": method, "status": status, "url": url}


def test_nudge_when_message_appeared_with_no_write():
    before, after = make_page_state(toast_messages=[]), make_page_state(toast_messages=["Saved!"])
    _, check = _reconcile_action([], before, after)
    assert check and "NO mutating request" in check


def test_nudge_and_error_evidence_on_failed_write():
    before, after = make_page_state(toast_messages=[]), make_page_state(toast_messages=["Task created"])
    evidence, check = _reconcile_action([_req("POST", 500)], before, after)
    assert any("HTTP 500" in e for e in evidence)
    assert check and "error" in check


def test_no_nudge_on_legit_success_with_write():
    before, after = make_page_state(toast_messages=[]), make_page_state(toast_messages=["Saved!"])
    _, check = _reconcile_action([_req("POST", 200)], before, after)
    assert check is None


def test_no_nudge_without_any_message():
    # No new message -> no nudge, regardless of requests (avoids noise on
    # legitimate client-side actions). The agent still sees request evidence.
    _, check = _reconcile_action([_req("GET", 200)], make_page_state(), make_page_state())
    assert check is None


def test_does_not_assert_deception():
    # The nudge is a CHECK, never an assertion — no "[FLAG]"/"deceptive"/"lying
    # toast" verdict baked into the evidence/check strings.
    before, after = make_page_state(toast_messages=[]), make_page_state(toast_messages=["Filter applied"])
    evidence, check = _reconcile_action([], before, after)
    blob = " ".join(evidence) + " " + (check or "")
    assert "[FLAG]" not in blob and "deceptive" not in blob.lower()


_PAGE = """<html><body>
<button id=s>Save</button>
<div id=t role="alert" style="display:none">Saved!</div>
<script>
document.getElementById('s').onclick = () => {
  fetch('/api/save', {method: 'POST'});
  document.getElementById('t').style.display = 'block';
};
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_PAGE.encode())

    def do_POST(self):
        self.send_response(500)  # the write fails on the server
        self.end_headers()


async def test_action_flags_fake_success_live():
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    start = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    end = m.end_session.fn if hasattr(m.end_session, "fn") else m.end_session
    ta = m.test_action.fn if hasattr(m.test_action, "fn") else m.test_action
    observe = m.observe.fn if hasattr(m.observe, "fn") else m.observe
    try:
        try:
            await start(base + "/page.html")
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        await observe()
        out = await ta("Save")
        # message over a 500 POST -> evidence shows the error + a CHECK nudge
        assert "CROSS-STACK" in out
        assert "CHECK:" in out
        assert "500" in out
    finally:
        await end()
        srv.shutdown()
