"""Live tests for state capsules: capture/restore a logged-in state with a
mandatory liveness check, and flag findings recorded against a stale capsule."""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

import argus.mcp_server as m


class _AuthHandler(http.server.BaseHTTPRequestHandler):
    """Shows 'Welcome Alice' only when the auth cookie is present."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        cookie = self.headers.get("Cookie", "")
        marker = "Welcome Alice" if "auth=alice" in cookie else "Please log in"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body><h1>{marker}</h1></body></html>".encode())


def _serve():
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), _AuthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def _start(url):
    fn = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    try:
        await fn(url)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


async def _call(tool, **kw):
    return await (getattr(tool, "fn", tool))(**kw)


async def test_capsule_live_roundtrip():
    srv, base = _serve()
    await _start(base + "/")
    try:
        s = m._session
        # "Log in" by setting the cookie the server gates on, then save.
        await s.browser.cookies_set([{"name": "auth", "value": "alice", "url": base}])
        await s.browser.goto(base + "/")
        assert "Welcome Alice" in (await s.browser.get_state()).page_text
        assert "Saved capsule" in await _call(m.capsule_save, name="acct",
                                              liveness_marker="Welcome Alice")
        # "Log out", confirm the marker is gone, then restore.
        await s.browser.cookies_clear()
        await s.browser.goto(base + "/")
        assert "Please log in" in (await s.browser.get_state()).page_text
        res = await _call(m.capsule_restore, name="acct")
        assert "LIVE" in res
        assert s._capsule_marker == "Welcome Alice"
        assert "Welcome Alice" in (await s.browser.get_state()).page_text
    finally:
        await _call(m.end_session)
        srv.shutdown()


async def test_restore_is_clean_replace_not_merge():
    # Storage present before restore but absent from the capsule must be cleared,
    # so a prior identity can't bleed into the restored one (NS-4).
    srv, base = _serve()
    await _start(base + "/")
    try:
        s = m._session
        await _call(m.capsule_save, name="empty")  # capsule has no storage
        await s.browser.storage_set("leftover", "from-identity-B", "local")
        assert "leftover" in await s.browser.storage_get("local")
        await _call(m.capsule_restore, name="empty")
        assert "leftover" not in await s.browser.storage_get("local")
    finally:
        await _call(m.end_session)
        srv.shutdown()


async def test_stale_capsule_flags_recorded_bug():
    srv, base = _serve()
    await _start(base + "/")  # never logged in -> marker will be absent on restore
    try:
        s = m._session
        await _call(m.capsule_save, name="ghost", liveness_marker="Welcome Alice")
        res = await _call(m.capsule_restore, name="ghost")
        assert "STALE" in res
        assert s._capsule_marker == "Welcome Alice"
        # Still logged out -> marker absent now -> record_bug flags it (fresh
        # re-check, not a sticky latch).
        await _call(m.record_bug, title="x", severity="low",
                    evidence={"screenshot": "skip"})
        assert "UNRELIABLE" in m._session.bugs[-1].description
    finally:
        await _call(m.end_session)
        srv.shutdown()
