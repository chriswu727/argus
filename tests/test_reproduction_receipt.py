"""Tests for the reproduction-receipt layer — Argus's anti-false-positive guard.

`_receipt_verdict` and `_resolve_url` are pure and tested directly. The
end-to-end path (record_bug independently re-confirming a symptom on a clean
load) runs against a self-contained file:// page and is skipped when Chromium
isn't installed.
"""
from __future__ import annotations

import functools
import http.server
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

import argus.mcp_server as m


# ── pure verdict logic ───────────────────────────────────────────────

def test_verdict_all_runs_match_is_reproduced():
    r = m._receipt_verdict([True, True], "present")
    assert r["reproduced"] is True and r["flaky"] is False and r["runs"] == "2/2"


def test_verdict_no_runs_match_is_not_reproduced():
    r = m._receipt_verdict([True, True], "absent")  # claimed absent, seen present
    assert r["reproduced"] is False and r["flaky"] is False and r["runs"] == "0/2"


def test_verdict_partial_match_is_flaky():
    r = m._receipt_verdict([True, False], "present")
    assert r["reproduced"] is False and r["flaky"] is True and r["runs"] == "1/2"


def test_verdict_absent_claim_confirmed_when_text_gone():
    r = m._receipt_verdict([False, False], "absent")
    assert r["reproduced"] is True and r["runs"] == "2/2"


# ── symptom matching is token-level, not bare substring ──────────────
# A boundary-free substring scan would stamp VERIFIED on a non-bug when the
# target text appears incidentally (inside a longer word or a longer item).

def test_token_present_requires_word_boundaries():
    assert m._token_present("category", "Browse by category here") is True
    assert m._token_present("cat", "Browse by category here") is False
    assert m._token_present("delete", "Recently Deleted: none") is False
    assert m._token_present("buy groceries", "  buy   groceries  ") is True  # ws-normalised


def test_text_in_state_does_not_match_incidental_substrings():
    from tests.conftest import make_page_state, make_element
    st = make_page_state(
        page_text="Browse by category. Recently deleted: none.",
        elements=[make_element(text="Deleted")],
        item_lists={"tasks": ["Buy groceries supplies"]},
    )
    assert m._text_in_state("cat", st) is False
    assert m._text_in_state("delete", st) is False
    assert m._text_in_state("category", st) is True
    assert m._text_in_state("Buy groceries", st) is True  # token run, even inside a longer item


# ── relative-URL resolution ──────────────────────────────────────────

class _FakePage:
    def __init__(self, url):
        self.url = url


class _FakeBrowser:
    def __init__(self, url):
        self._page = _FakePage(url)


class _FakeSession:
    def __init__(self, url):
        self.browser = _FakeBrowser(url)


def test_resolve_url_joins_relative_against_current_origin():
    s = _FakeSession("http://127.0.0.1:5555/account")
    assert m._resolve_url(s, "/tasks") == "http://127.0.0.1:5555/tasks"


def test_resolve_url_passes_absolute_through():
    s = _FakeSession("http://127.0.0.1:5555/account")
    assert m._resolve_url(s, "http://other.test/x") == "http://other.test/x"


# ── end-to-end: independent re-confirmation on a clean load ──────────

_PAGE = "<html><body><h1>Tasks</h1><ul><li>Buy groceries</li></ul></body></html>"


async def _session_on_page(html: str):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    url = Path(f.name).as_uri()
    try:
        await m.start_session.fn(url) if hasattr(m.start_session, "fn") else await m.start_session(url)
    except Exception as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")
    return url


async def _record(**kw):
    fn = getattr(m.record_bug, "fn", m.record_bug)
    return await fn(**kw)


async def _end():
    fn = getattr(m.end_session, "fn", m.end_session)
    await fn()


async def test_receipt_confirms_true_symptom_and_flags_false_one():
    await _session_on_page(_PAGE)
    try:
        # Real symptom present on the page -> reproduced.
        await _record(title="real", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "Buy groceries"})
        # Claimed present but not on the page -> must NOT reproduce.
        await _record(title="bogus", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "ZZZ-NOT-REAL"})
        # No verify clause -> observation-based, receipt stays None.
        await _record(title="visual", severity="low", evidence={"screenshot": "skip"})

        s = getattr(m, "_session", None) or m._require_session()
        by_title = {b.title: b for b in s.bugs}
        assert by_title["real"].reproduction_receipt["reproduced"] is True
        assert by_title["bogus"].reproduction_receipt["reproduced"] is False
        assert by_title["visual"].reproduction_receipt is None
    finally:
        await _end()


# ── divergence: the receipt must read SERVER truth, not the live DOM ──
# A file:// page can't show this — the current DOM and a fresh GET are
# byte-identical, so the E2E would pass even if the re-check never reloaded.
# These use a stateful HTTP server and mutate the live DOM out from under the
# receipt, then assert the verdict follows the server, not the stale client.


class _StatefulHandler(http.server.BaseHTTPRequestHandler):
    persisted = "Persisted Item"
    flaky_hits = 0

    def log_message(self, *a):
        pass

    def _send(self, body: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/flaky"):
            # Toggle FlakyText on every GET, so two consecutive re-check loads
            # always disagree -> flaky.
            type(self).flaky_hits += 1
            shown = "FlakyText" if type(self).flaky_hits % 2 == 0 else "stable only"
            self._send(f"<html><body><p>{shown}</p></body></html>")
        else:
            self._send(f"<html><body><ul><li>{type(self).persisted}</li></ul></body></html>")


def _start_server():
    class _Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _Srv(("127.0.0.1", 0), _StatefulHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def _start_session_url(url: str):
    try:
        await (m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session)(url)
    except Exception as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")


async def test_receipt_follows_server_not_stale_client_dom():
    _StatefulHandler.flaky_hits = 0
    srv, base = _start_server()
    await _start_session_url(base + "/")
    try:
        page = m._session.browser._page
        # Mutate the live DOM away from server truth: drop the persisted item,
        # inject a phantom one. A receipt that scanned the current DOM would be
        # fooled; one that re-GETs the server must not be.
        await page.evaluate(
            "() => { document.querySelector('li').remove();"
            " const li=document.createElement('li'); li.textContent='Phantom Item';"
            " document.querySelector('ul').appendChild(li); }"
        )

        # Claim it's absent — but the server still serves it -> must NOT confirm.
        await _record(title="A", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "absent", "target_text": "Persisted Item"})
        # Claim the phantom is present — server never had it -> must NOT confirm.
        await _record(title="B", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "Phantom Item"})
        # Sanity: the server-true symptom DOES confirm.
        await _record(title="C", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "Persisted Item"})

        by_title = {b.title: b for b in m._session.bugs}
        assert by_title["A"].reproduction_receipt["reproduced"] is False
        assert by_title["B"].reproduction_receipt["reproduced"] is False
        assert by_title["C"].reproduction_receipt["reproduced"] is True
    finally:
        await _end()
        srv.shutdown()


async def test_receipt_flags_intermittent_symptom_as_flaky():
    _StatefulHandler.flaky_hits = 0
    srv, base = _start_server()
    await _start_session_url(base + "/flaky")
    try:
        await _record(title="flaky", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "FlakyText", "at_url": "/flaky"})
        receipt = m._session.bugs[-1].reproduction_receipt
        assert receipt["flaky"] is True
        assert receipt["reproduced"] is False
        assert receipt["runs"] == "1/2"
    finally:
        await _end()
        srv.shutdown()


async def test_receipt_reports_none_on_navigation_failure():
    srv, base = _start_server()
    await _start_session_url(base + "/")
    try:
        await _record(title="naverr", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "x",
                              "at_url": "http://127.0.0.1:1/unreachable"})
        receipt = m._session.bugs[-1].reproduction_receipt
        assert receipt["reproduced"] is None
        assert "error" in receipt
    finally:
        await _end()
        srv.shutdown()
