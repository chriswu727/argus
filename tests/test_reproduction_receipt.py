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


class _LoginWallHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Please log in to continue</h1></body></html>")


async def test_item_lists_excludes_hidden_rows():
    # observe must report only what a human sees: a display:none row must not
    # appear in item_lists (it already doesn't in page_text / interactive els).
    page = ('<html><body><ul>'
            '<li>VisibleAlpha</li>'
            '<li style="display:none">HiddenBeta</li>'
            '<li>VisibleGamma</li>'
            '</ul></body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        joined = " ".join(v for lst in st.item_lists.values() for v in lst)
        assert "VisibleAlpha" in joined and "VisibleGamma" in joined
        assert "HiddenBeta" not in joined  # hidden row excluded
    finally:
        await _end()


class _IframeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/frame"):
            self._send('<html><body><label for="card">Card number</label>'
                       '<input id="card"><button id="pay">Pay Now</button></body></html>')
        else:
            self._send('<html><body><button id="outer">Outer</button>'
                       '<iframe id="pay" src="/frame" width="320" height="200"></iframe></body></html>')


async def test_same_origin_iframe_observed_and_interactive():
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    srv = _S(("127.0.0.1", 0), _IframeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    await _start_session_url(base + "/")
    try:
        obs = await (getattr(m.observe, "fn", m.observe))()
        # elements INSIDE the same-origin iframe must be surfaced by observe
        assert "Card number" in obs and "Pay Now" in obs
        # and actually reachable — type_into / click_what land in the frame
        await (getattr(m.type_into, "fn", m.type_into))(description="Card number field", text="4242")
        r = await (getattr(m.click_what, "fn", m.click_what))(description="Pay Now button")
        assert "Clicked" in r
        # confirm the value truly landed in the IFRAME input (not silently dropped)
        val = await m._session.browser._page.frame_locator('iframe[id="pay"]').locator("#card").input_value()
        assert val == "4242"
    finally:
        await _end()
        srv.shutdown()


async def test_click_settles_async_dom_mutation():
    # A click that triggers a DELAYED (setTimeout, no network) DOM update: observe
    # right after must see the update, i.e. click() settled the DOM first.
    page = ('<html><body><button id="go" onclick="setTimeout(function(){'
            "var p=document.createElement('p');p.textContent='DELAYED_MARKER';"
            'document.body.appendChild(p);},150)">Go</button></body></html>')
    await _session_on_page(page)
    try:
        cw = getattr(m.click_what, "fn", m.click_what)
        ob = getattr(m.observe, "fn", m.observe)
        await cw(description="Go button")
        obs = await ob()
        assert "DELAYED_MARKER" in obs  # settle waited past the 400ms setTimeout
    finally:
        await _end()


async def test_record_bug_tolerates_string_evidence():
    # A weaker agent passes `evidence` as a bare string — used to crash
    # record_bug ('str' has no .get) and silently lose the finding.
    await _session_on_page(_PAGE)
    try:
        out = await _record(title="strEv", severity="low",
                            evidence="clicked delete but the item stayed")
        assert "Recorded bug" in out
        assert m._session.bugs[-1].description == "clicked delete but the item stayed"
    finally:
        await _end()


async def test_verify_can_be_carried_in_evidence():
    # Lower-barrier path: the agent puts the checkable target in `evidence`
    # (no separate verify dict) and the moat still engages.
    await _session_on_page(_PAGE)
    try:
        await _record(title="via-evidence", severity="low",
                      evidence={"screenshot": "skip", "target_text": "Buy groceries", "expect": "present"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r is not None and r["reproduced"] is True
    finally:
        await _end()


async def test_clean_load_login_wall_is_inconclusive():
    # Session expired -> the clean GET hits a login wall, so an expect=absent
    # target is absent because we're logged out, NOT because the bug is fixed.
    # Must be INCONCLUSIVE, never a false VERIFIED.
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    srv = _S(("127.0.0.1", 0), _LoginWallHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    await _start_session_url(base + "/")
    try:
        await _record(title="x", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "absent", "target_text": "some-task", "at_url": "/"})
        r = m._session.bugs[-1].reproduction_receipt
        assert r["reproduced"] is None
        assert "login wall" in r.get("reason", "")
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
