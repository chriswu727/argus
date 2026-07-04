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


async def test_cross_origin_iframe_observed_and_interactive():
    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    inner = _S(("127.0.0.1", 0), _IframeHandler)  # serves /frame -> card input + Pay
    threading.Thread(target=inner.serve_forever, daemon=True).start()
    inner_port = inner.server_address[1]

    class _OuterHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((
                '<html><body><button id="outer">Outer</button>'
                f'<iframe id="pay" src="http://127.0.0.1:{inner_port}/frame" width="320" height="200">'
                '</iframe></body></html>').encode())

    outer = _S(("127.0.0.1", 0), _OuterHandler)
    threading.Thread(target=outer.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{outer.server_address[1]}"  # different port => cross-origin
    await _start_session_url(base + "/")
    try:
        obs = await (getattr(m.observe, "fn", m.observe))()
        # cross-origin frame CONTENTS are now surfaced (were a "blind spot" marker)
        assert "Card number" in obs and "Pay Now" in obs
        await (getattr(m.type_into, "fn", m.type_into))(description="Card number field", text="4111")
        val = await m._session.browser._page.frame_locator('iframe[id="pay"]').locator("#card").input_value()
        assert val == "4111"
    finally:
        await _end()
        inner.shutdown()
        outer.shutdown()


async def test_canvas_surfaced_and_click_at_lands():
    page = ('<html><body>'
            '<canvas id="c" width="200" height="150" style="width:200px;height:150px"></canvas>'
            '<div id="out">none</div>'
            "<script>document.getElementById('c').addEventListener('click',function(e){"
            "document.getElementById('out').textContent='CLICKED_CANVAS';});</script>"
            '</body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        assert st.canvases and st.canvases[0]["w"] >= 40  # canvas region surfaced with its rect
        cx, cy = st.canvases[0]["x"], st.canvases[0]["y"]
        await (getattr(m.click_at, "fn", m.click_at))(x=cx, y=cy)
        st2 = await m._session.browser.get_state()
        assert "CLICKED_CANVAS" in st2.page_text  # the coordinate click landed on the canvas
        obs = await (getattr(m.observe, "fn", m.observe))()
        assert "Canvas regions" in obs and "click_at" in obs  # observe flags it + the escape hatch
    finally:
        await _end()


async def test_click_at_below_fold_canvas_scrolls_and_lands():
    # click_at on a below-the-fold canvas used to silently miss but report success.
    page = ('<html><body><div style="height:1600px">spacer</div>'
            '<canvas id="c" width="200" height="150" style="width:200px;height:150px"></canvas>'
            '<div id="out">none</div>'
            "<script>document.getElementById('c').addEventListener('click',function(e){"
            "document.getElementById('out').textContent='HIT_BELOW';});</script>"
            '</body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        cv = st.canvases[0]
        vph = await m._session.browser._page.evaluate("() => window.innerHeight")
        assert cv["y"] > vph  # canvas center is below the fold
        ok_msg = await (getattr(m.click_at, "fn", m.click_at))(x=cv["x"], y=cv["y"])
        assert "Clicked" in ok_msg
        st2 = await m._session.browser.get_state()
        assert "HIT_BELOW" in st2.page_text  # it scrolled into view and actually landed
    finally:
        await _end()


async def test_record_bug_string_steps_not_char_split():
    await _session_on_page('<html><body><button>X</button></body></html>')
    try:
        rb = getattr(m.record_bug, "fn", m.record_bug)
        # a newline-joined string (a very common model shape) -> one step per line
        await rb(title="Newline steps bug", severity="low",
                 evidence={"steps": "Open the page\nClick X\nSee the error"})
        assert m._session.bugs[-1].steps_to_reproduce == ["Open the page", "Click X", "See the error"]
        # a single string with no newlines -> one step, NOT one <li> per character
        await rb(title="One-line steps bug", severity="low",
                 evidence={"steps": "Just one step here"})
        assert m._session.bugs[-1].steps_to_reproduce == ["Just one step here"]
        # inline-numbered run-on "1. a 2. b 3. c" -> three clean steps (no numbers)
        await rb(title="Inline numbered bug", severity="low",
                 evidence={"steps": "1. Open /new 2. Fill title 3. Click Save"})
        assert m._session.bugs[-1].steps_to_reproduce == ["Open /new", "Fill title", "Click Save"]
    finally:
        await _end()


async def test_observe_shows_invalid_and_selected_state():
    from argus.resolver import describe
    page = ('<html><body>'
            '<input type="email" aria-invalid="true" aria-label="Email" value="not-an-email">'
            '<input type="email" aria-label="Native" value="bad@" required>'
            '<input type="text" aria-label="Empty" required>'
            '<div role="tab" aria-selected="true" aria-label="Tab1">Tab1</div></body></html>')
    await _session_on_page(page)
    try:
        ds = {e.aria_label: describe(e) for e in (await m._session.browser.get_state()).elements}
        assert "[invalid]" in ds["Email"]           # explicit aria-invalid
        assert "[invalid]" in ds["Native"]          # native :invalid on a FILLED field
        assert "[invalid]" not in ds["Empty"]       # empty required -> not flagged (no noise)
        assert "[selected]" in ds["Tab1"]
    finally:
        await _end()


async def test_observe_shows_aria_expanded_pressed_current():
    from argus.resolver import describe
    page = ('<html><body>'
            '<button aria-expanded="false" aria-label="Filters">Filters</button>'
            '<button aria-expanded="true" aria-label="Menu">Menu</button>'
            '<button aria-pressed="true" aria-label="Bold">B</button>'
            '<a href="#" aria-current="page" aria-label="Home">Home</a></body></html>')
    await _session_on_page(page)
    try:
        ds = {e.aria_label: describe(e) for e in (await m._session.browser.get_state()).elements}
        assert "[collapsed]" in ds["Filters"] and "[expanded]" in ds["Menu"]
        assert "[pressed]" in ds["Bold"] and "[current]" in ds["Home"]
    finally:
        await _end()


async def test_observe_select_shows_label_and_selected_option():
    from argus.resolver import describe
    page = ('<html><body>'
            '<select aria-label="Sort"><option value="az">Name A-Z</option>'
            '<option value="za" selected>Name Z-A</option></select>'
            '<select><option>USA</option><option selected>Canada</option></select></body></html>')
    await _session_on_page(page)
    try:
        joined = " | ".join(describe(e) for e in (await m._session.browser.get_state()).elements if e.tag == "select")
        assert 'select "Sort" = "Name Z-A"' in joined     # labelled: label + current selection
        assert 'select "Canada"' in joined                # unlabelled: selection is the label
        assert "Name A-Z" not in joined and "USA" not in joined  # NOT all options mashed together
    finally:
        await _end()


async def test_observe_keeps_opacity0_restyled_checkbox():
    # TodoMVC/Bootstrap/Material pattern: a real checkbox at opacity:0 behind a
    # styled visual — must stay targetable (the opacity filter used to hide it,
    # making the whole widget untargetable on real apps).
    page = ('<html><body>'
            '<label><input type="checkbox" style="opacity:0" aria-label="Accept terms" checked>Accept</label>'
            '<button style="opacity:0">HiddenBtn</button>'
            '</body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        labels = " ".join((e.aria_label or e.text or "") for e in st.elements)
        assert "Accept terms" in labels    # opacity:0 checkbox is KEPT
        assert "HiddenBtn" not in labels    # opacity:0 non-control button still filtered
        cb = [e for e in st.elements if e.type == "checkbox"]
        assert cb and cb[0].checked is True
    finally:
        await _end()


async def test_observe_distinguishes_checked_checkboxes():
    from argus.resolver import describe
    page = ('<html><body>'
            '<label>Remember me<input type="checkbox" checked></label>'
            '<label>Subscribe<input type="checkbox"></label></body></html>')
    await _session_on_page(page)
    try:
        cbs = [e for e in (await m._session.browser.get_state()).elements if e.type == "checkbox"]
        assert len(cbs) == 2
        assert {e.checked for e in cbs} == {True, False}   # live checked state captured
        ds = " ".join(describe(e) for e in cbs)
        assert "[checked]" in ds and "[unchecked]" in ds    # and shown distinctly
    finally:
        await _end()


async def test_paste_into_fires_handler_and_default_insert():
    page = ('<html><body>'
            '<input id="h" placeholder="Coupon">'
            '<input id="p" placeholder="Plain">'
            "<script>document.getElementById('h').addEventListener('paste',function(e){"
            "e.preventDefault();var t=e.clipboardData.getData('text');this.value='PASTED:'+t.toUpperCase();});"
            '</script></body></html>')
    await _session_on_page(page)
    try:
        pi = getattr(m.paste_into, "fn", m.paste_into)
        await pi(description="Coupon field", text="save10")
        await pi(description="Plain field", text="hello123")
        vh = await m._session.browser._page.locator("#h").input_value()
        vp = await m._session.browser._page.locator("#p").input_value()
        assert vh == "PASTED:SAVE10"   # onpaste handler fired and transformed the paste
        assert vp == "hello123"        # no handler -> default insert still worked
    finally:
        await _end()


async def test_drop_file_onto_dropzone_with_real_bytes():
    import tempfile
    import os as _os
    fd, fpath = tempfile.mkstemp(suffix=".txt")
    _os.write(fd, b"HELLO_DROPZONE_REAL_BYTES_PAYLOAD")
    _os.close(fd)
    real_size = str(_os.path.getsize(fpath))
    page = ('<html><body>'
            '<div id="drop" role="button" tabindex="0">Drop files here</div>'
            '<div id="result">no file</div>'
            "<script>var d=document.getElementById('drop');"
            "d.addEventListener('dragover',function(e){e.preventDefault();});"
            "d.addEventListener('drop',function(e){e.preventDefault();var f=e.dataTransfer.files[0];"
            "document.getElementById('result').textContent='GOT:'+f.name+':'+f.size;});"
            '</script></body></html>')
    await _session_on_page(page)
    try:
        await (getattr(m.drop_file, "fn", m.drop_file))(description="Drop files here", path=fpath)
        pt = (await m._session.browser.get_state()).page_text
        assert "GOT:" in pt and _os.path.basename(fpath) in pt   # dropzone handler got a real File
        assert real_size in pt   # the ACTUAL byte size reached the handler (not a fabricated stub)
    finally:
        await _end()
        _os.unlink(fpath)


async def test_click_does_not_fail_on_click_triggered_hang():
    import time as _time

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/hang":
                _time.sleep(30)  # the click's fetch never resolves
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<html><body><button id="b" onclick="go()">Load data</button>'
                             b'<div id="r">idle</div>'
                             b"<script>function go(){document.getElementById('r').textContent='CLICK_DONE';"
                             b"fetch('/hang');}</script></body></html>")

    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    srv = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    await _start_session_url(f"http://127.0.0.1:{srv.server_address[1]}/")
    try:
        res = await (getattr(m.click_what, "fn", m.click_what))(description="Load data button")
        assert "failed" not in res.lower()  # click succeeded despite the hanging fetch it started
        assert "CLICK_DONE" in (await m._session.browser.get_state()).page_text
    finally:
        await _end()
        srv.shutdown()


async def test_goto_survives_never_resolving_fetch():
    import time as _time

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/hang":
                _time.sleep(30)  # never responds within the test -> networkidle never fires
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<html><body><div id="s">Loading forever spinner</div>'
                             b"<script>fetch('/hang').then(r=>r.text()).then(t=>{"
                             b"document.getElementById('s').textContent=t;});</script></body></html>")

    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    srv = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    t0 = _time.time()
    await _start_session_url(base)
    try:
        assert _time.time() - t0 < 20  # did NOT hang the full 30s on the pending fetch
        obs = await (getattr(m.observe, "fn", m.observe))()
        assert "Loading forever spinner" in obs  # the stuck-loading state is observable
    finally:
        await _end()
        srv.shutdown()


async def test_receipt_scroll_search_finds_virtualized_row():
    # Window-virtualized list: only rows near window.scrollY are in the DOM.
    page = ('<html><body><div id="c" style="height:5000px;position:relative"></div>'
            "<script>var rows=[];for(var i=0;i<100;i++)rows.push(i==80?'ROW_TARGET':('Row '+i));"
            "function render(){var top=window.scrollY;var start=Math.floor(top/50);"
            "var end=start+Math.ceil(window.innerHeight/50)+2;var c=document.getElementById('c');c.innerHTML='';"
            "for(var i=Math.max(0,start);i<end&&i<rows.length;i++){var d=document.createElement('div');"
            "d.style.position='absolute';d.style.top=(i*50)+'px';d.textContent=rows[i];c.appendChild(d);}}"
            "window.addEventListener('scroll',render);render();</script></body></html>")
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        assert not m._text_in_state("ROW_TARGET", st)          # row 80 not rendered initially
        assert await m._present_with_scroll(m._session, "ROW_TARGET", st) is True   # scroll-search finds it
        st2 = await m._session.browser.get_state()
        assert await m._present_with_scroll(m._session, "NOPE_NOT_A_ROW", st2) is False  # truly absent stays absent
    finally:
        await _end()


async def test_verify_persistence_clear_storage_catches_localonly_save():
    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((
                '<html><body><input id="v" placeholder="Value">'
                '<button id="save" onclick="save()">Save</button><div id="disp"></div>'
                "<script>function save(){localStorage.setItem('val',document.getElementById('v').value);render();}"
                "function render(){var x=localStorage.getItem('val');document.getElementById('disp').textContent=x?('STORED:'+x):'';}"
                "render();</script></body></html>").encode())

    class _S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    srv = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    await _start_session_url(base)
    try:
        await (getattr(m.type_into, "fn", m.type_into))(description="Value field", text="MAGICVAL")
        await (getattr(m.click_what, "fn", m.click_what))(description="Save button")
        vp = getattr(m.verify_persistence, "fn", m.verify_persistence)
        # default reload keeps localStorage -> the local-only save reads persisted
        r1 = await vp(expect="present", target_text="STORED:MAGICVAL")
        assert "MATCH" in r1 and "MISMATCH" not in r1
        # clear_storage proves SERVER persistence -> the local-only save is gone
        r2 = await vp(expect="present", target_text="STORED:MAGICVAL", clear_storage=True)
        assert "MISMATCH" in r2
    finally:
        await _end()
        srv.shutdown()


async def test_download_capture_and_verify_broken_export():
    # "Export CSV" writes ONLY the header (a broken export) — get_downloads must
    # surface it so a tester can catch the missing data rows.
    page = ('<html><body><button id="exp" onclick="dl()">Export CSV</button>'
            '<script>function dl(){'
            "var b=new Blob(['Name,Amount\\n'],{type:'text/csv'});"
            "var a=document.createElement('a');a.href=URL.createObjectURL(b);"
            "a.download='report.csv';document.body.appendChild(a);a.click();}"
            '</script></body></html>')
    await _session_on_page(page)
    try:
        await (getattr(m.click_what, "fn", m.click_what))(description="Export CSV button")
        out = await (getattr(m.get_downloads, "fn", m.get_downloads))()
        assert "report.csv" in out
        assert "Name,Amount" in out    # preview reveals the header-only content
        assert "SUSPICIOUS" in out     # 12 bytes < 50 -> flagged as likely-broken
    finally:
        await _end()


async def test_resize_sweeps_responsive_breakpoint():
    page = ('<html><head><style>'
            '.mobile{display:none}@media(max-width:768px){.desktop{display:none}.mobile{display:block}}'
            '</style></head><body>'
            '<button class="desktop">DesktopNav</button>'
            '<button class="mobile">Hamburger</button></body></html>')
    await _session_on_page(page)
    try:
        rz = getattr(m.resize, "fn", m.resize)
        await rz(width=1280, height=800)
        l1 = " ".join((e.text or "") for e in (await m._session.browser.get_state()).elements)
        assert "DesktopNav" in l1 and "Hamburger" not in l1     # desktop layout
        await rz(width=375, height=800)
        l2 = " ".join((e.text or "") for e in (await m._session.browser.get_state()).elements)
        assert "Hamburger" in l2 and "DesktopNav" not in l2      # mobile layout after resize
    finally:
        await _end()


async def test_observe_excludes_opacity0_ancestor_subtree():
    # opacity is not inherited, so a button inside an opacity:0 fade-menu has its
    # OWN opacity 1 — it used to leak into observe. checkVisibility catches it.
    page = ('<html><body>'
            '<div style="opacity:0"><button>HiddenInFade</button></div>'
            '<button>VisibleBtn2</button></body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        labels = " ".join((e.text or "") for e in st.elements)
        assert "VisibleBtn2" in labels
        assert "HiddenInFade" not in labels  # hidden by an ancestor's opacity:0
    finally:
        await _end()


async def test_observe_fidelity_hidden_widgets_disabled():
    page = ('<html><body>'
            '<button id="vis">VisibleBtn</button>'
            '<button style="opacity:0">TransparentBtn</button>'
            '<a href="#" style="position:absolute;left:-9999px">OffScreenLink</a>'
            '<div contenteditable="true">EditableArea</div>'
            '<div role="switch" aria-checked="false" tabindex="0">ToggleSwitch</div>'
            '<button disabled>DisabledBtn</button>'
            '</body></html>')
    await _session_on_page(page)
    try:
        st = await m._session.browser.get_state()
        labels = " ".join((e.text or e.aria_label or e.name or "") for e in st.elements)
        assert "VisibleBtn" in labels
        assert "TransparentBtn" not in labels   # opacity:0 -> excluded from interactive els
        assert "OffScreenLink" not in labels     # left:-9999px -> excluded
        assert "EditableArea" in labels          # contenteditable now enumerated
        assert "ToggleSwitch" in labels          # role=switch now enumerated
        assert any(e.disabled and "DisabledBtn" in (e.text or "") for e in st.elements)
        obs = await (getattr(m.observe, "fn", m.observe))()
        assert "[disabled]" in obs               # disabled state surfaced in observe
    finally:
        await _end()


async def test_press_key_escape_dismisses_modal():
    # The canonical keyboard case click/type can't do: Escape closes a modal.
    page = ('<html><body><div id="m">MODAL_OPEN</div>'
            "<script>document.addEventListener('keydown',function(e){"
            "if(e.key==='Escape'){document.getElementById('m').style.display='none';}});</script>"
            '</body></html>')
    await _session_on_page(page)
    try:
        pk = getattr(m.press_key, "fn", m.press_key)
        ob = getattr(m.observe, "fn", m.observe)
        assert "MODAL_OPEN" in await ob()
        await pk(key="Escape")
        assert "MODAL_OPEN" not in await ob()  # Escape hid the modal
    finally:
        await _end()


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
