"""An action lands on the element that was resolved, or fails."""

from __future__ import annotations

import http.server
import threading

import pytest

from argus.application.actions import click_what, type_into
from argus.application.journeys import test_form as run_form
from argus.application.lifecycle import start_session

LOG = "<pre id='log'></pre><script>function log(t){document.getElementById('log').textContent+=t+';'}</script>"

PAGES = {
    "/substring": LOG
    + "<button onclick=\"log('ALL')\">Delete all</button><button onclick=\"log('ONE')\">Delete</button>",
    "/hover": LOG
    + "<style>li button{opacity:0}li:hover button{opacity:1}</style><ul>"
    + "".join(
        f"<li>{name} <button onclick=\"log('{name}')\">Delete</button></li>"
        for name in ("Buy groceries", "Pay rent", "Walk dog")
    )
    + "</ul>",
    "/title": LOG
    + "<button title='Remove' onclick=\"log('TITLE')\">x</button>"
    + "<div><div onclick=\"log('CARD')\">Open card</div></div>"
    + "<input placeholder='Enter your full legal name exactly as it appears on your passport document'>",
    "/forms": LOG
    + "<ul><li>Buy milk <button onclick=\"log('DELETE')\">Delete</button></li></ul>"
    + "<form onsubmit=\"log('NEWS '+this.email.value);return false\"><input name='email' aria-label='Newsletter email'><button name='action'>Subscribe</button></form>"
    + "<form onsubmit=\"log('ACCOUNT '+this.email.value);return false\"><input name='email' aria-label='Account email'><button name='action'>Save account</button></form>",
    "/moving": LOG
    + "<ul id='list'>"
    + "".join(
        f"<li>{name} <button onclick=\"log('{name}')\">Delete</button></li>"
        for name in ("Buy milk", "Pay rent")
    )
    + "</ul><button onclick=\"document.getElementById('list').insertAdjacentHTML('afterbegin',"
    + "'<li>Urgent <button>Delete</button></li>')\">Prepend</button>"
    + "<button onclick=\"document.getElementById('list').innerHTML=''\">Clear</button>",
}


@pytest.fixture
def base():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGES.get(self.path, "<p>home</p>").encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def _log(runtime) -> str:
    return await runtime.session.browser._page.inner_text("#log")


async def test_label_that_is_a_substring_of_an_earlier_one(isolated_runtime, base):
    try:
        await start_session(base + "/substring")
        await click_what("Delete")
        assert await _log(isolated_runtime) == "ONE;"
    finally:
        await isolated_runtime.shutdown()


async def test_row_control_shown_only_on_hover(isolated_runtime, base):
    try:
        await start_session(base + "/hover")
        browser = isolated_runtime.session.browser
        await browser._page.hover("li:nth-child(3)")
        state = await browser.get_state()
        index = next(i for i, el in enumerate(state.elements) if el.text == "Delete")
        assert await browser.click(index, state.elements)
        assert await _log(isolated_runtime) == "Walk dog;"
    finally:
        await isolated_runtime.shutdown()


async def test_labels_taken_from_title_bare_tags_and_long_placeholders(isolated_runtime, base):
    try:
        await start_session(base + "/title")
        await click_what("Remove")
        await click_what("Open card")
        await type_into("Enter your full legal name", "Ada")
        assert await _log(isolated_runtime) == "TITLE;CARD;"
        assert await isolated_runtime.session.browser._page.input_value("input") == "Ada"
    finally:
        await isolated_runtime.shutdown()


async def test_form_fills_and_submits_the_form_it_was_given(isolated_runtime, base):
    try:
        await start_session(base + "/forms")
        await run_form({"Account email": "me@example.test"})
        assert await _log(isolated_runtime) == "ACCOUNT me@example.test;"
    finally:
        await isolated_runtime.shutdown()


async def test_reference_follows_its_node_and_fails_once_the_node_is_gone(isolated_runtime, base):
    try:
        await start_session(base + "/moving")
        browser = isolated_runtime.session.browser
        state = await browser.get_state()
        index = next(
            i
            for i, el in enumerate(state.elements)
            if el.text == "Delete" and "Pay rent" in (el.parent_context or "")
        )
        await browser._page.click("text=Prepend")
        assert await browser.click(index, state.elements)
        assert await _log(isolated_runtime) == "Pay rent;"

        stale = await browser.get_state()
        gone = next(i for i, el in enumerate(stale.elements) if el.text == "Delete")
        await browser._page.click("text=Clear")
        assert not await browser.click(gone, stale.elements)
    finally:
        await isolated_runtime.shutdown()
