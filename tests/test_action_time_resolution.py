"""A description is resolved against the page as it is when the action runs."""

from __future__ import annotations

import http.server
import threading

import pytest

from argus.application.actions import click_what, hover_what
from argus.application.lifecycle import start_session
from argus.application.observation import observe
from argus.domain.contracts import ErrorCode, Rejection

LOG = "<pre id='log'></pre><script>function log(t){document.getElementById('log').textContent+=t+';'}</script>"

ROWS = (
    "<ul id='list'>"
    + "".join(
        f'<li><span>{name}</span> <button onclick="log(this.parentNode.firstChild.textContent)">'
        "Delete</button></li>"
        for name in ("Buy milk", "Pay rent")
    )
    + "</ul>"
)

PAGES = {
    "/rows": LOG + ROWS,
    "/archive": LOG
    + "<section id='inbox'><h2>Inbox</h2><button onclick=\"log('INBOX')\">Archive</button></section>"
    + "<section id='sent'><h2>Sent</h2></section><button onclick=\"log('COMPOSE')\">Compose</button>",
    "/hover": LOG
    + "<style>li button{display:none}li:hover button{display:inline}</style><ul>"
    + "".join(
        f"<li><span>{name}</span> <button onclick=\"log('{name}')\">Remove</button></li>"
        for name in ("Buy groceries", "Pay rent")
    )
    + "</ul>",
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


async def _run(runtime, script: str) -> None:
    await runtime.session.browser._page.evaluate(script)


async def test_a_row_prepended_after_the_observation(isolated_runtime, base):
    try:
        await start_session(base + "/rows")
        await observe()
        await _run(
            isolated_runtime,
            "document.getElementById('list').insertAdjacentHTML('afterbegin',"
            "'<li><span>Urgent</span> <button onclick=\"log(\\'Urgent\\')\">Delete</button></li>')",
        )
        result = await click_what("Delete near Pay rent")
        assert not isinstance(result, Rejection), result
        assert await _log(isolated_runtime) == "Pay rent;"
    finally:
        await isolated_runtime.shutdown()


async def test_the_same_nodes_now_show_other_rows(isolated_runtime, base):
    try:
        await start_session(base + "/rows")
        await observe()
        await _run(
            isolated_runtime,
            "const [a, b] = document.querySelectorAll('#list li span');"
            "[a.textContent, b.textContent] = [b.textContent, a.textContent];",
        )
        result = await click_what("Delete near Pay rent")
        assert not isinstance(result, Rejection), result
        assert await _log(isolated_runtime) == "Pay rent;"
    finally:
        await isolated_runtime.shutdown()


async def test_unique_when_observed_ambiguous_now(isolated_runtime, base):
    try:
        await start_session(base + "/archive")
        await observe()
        await _run(
            isolated_runtime,
            "document.getElementById('sent').insertAdjacentHTML('beforeend',"
            "'<button onclick=\"log(\\'SENT\\')\">Archive</button>')",
        )
        result = await click_what("Archive")
        assert isinstance(result, Rejection)
        assert result.code == ErrorCode.INVALID_ARGUMENT
        assert "ambiguous" in result
        assert await _log(isolated_runtime) == ""
    finally:
        await isolated_runtime.shutdown()


async def test_an_element_removed_since_the_observation(isolated_runtime, base):
    try:
        await start_session(base + "/archive")
        await observe()
        await _run(isolated_runtime, "document.querySelector('#inbox button').remove()")
        result = await click_what("Archive")
        assert isinstance(result, Rejection)
        assert "No element matches" in result
        assert await _log(isolated_runtime) == ""
    finally:
        await isolated_runtime.shutdown()


async def test_a_row_removed_since_the_observation_is_not_matched_by_another_row(
    isolated_runtime, base
):
    try:
        await start_session(base + "/rows")
        await observe()
        await _run(isolated_runtime, "document.querySelectorAll('#list li')[1].remove()")
        result = await click_what("Delete near Pay rent")
        assert isinstance(result, Rejection)
        assert await _log(isolated_runtime) == ""
    finally:
        await isolated_runtime.shutdown()


async def test_a_control_revealed_by_hover_is_clickable_next(isolated_runtime, base):
    try:
        await start_session(base + "/hover")
        await observe()
        await hover_what("Pay rent")
        result = await click_what("Remove")
        assert not isinstance(result, Rejection), result
        assert await _log(isolated_runtime) == "Pay rent;"
    finally:
        await isolated_runtime.shutdown()
