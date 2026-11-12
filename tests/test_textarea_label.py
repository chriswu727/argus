"""A <textarea>'s content is its value, never its label."""

from __future__ import annotations

import http.server
import threading

import pytest

from argus.application.actions import type_into
from argus.application.lifecycle import start_session
from argus.domain.contracts import Rejection
from argus.resolver import describe, resolve_element

from .conftest import make_element

PAGE = (
    "<label for='notes'>Notes</label><textarea id='notes'>Meeting at 3</textarea>"
    "<textarea name='api_key'>sk-live-0123456789abcdef</textarea>"
    "<textarea placeholder='Write a comment'></textarea>"
    "<label>Bio <textarea name='bio'>Hello there</textarea></label>"
)


@pytest.fixture
def base():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_textarea_content_is_extracted_as_value(isolated_runtime, base):
    try:
        await start_session(base + "/")
        browser = isolated_runtime.session.browser
        fields = [el for el in await browser.get_elements() if el.tag == "textarea"]
        assert [(el.text, el.aria_label, el.value) for el in fields] == [
            (None, "Notes", "Meeting at 3"),
            (None, None, "sk-live-0123456789abcdef"),
            (None, None, None),
            (None, "Bio", "Hello there"),
        ]
        assert [describe(el) for el in fields] == [
            'input "Notes" = "Meeting at 3"',
            'input "api_key" = (value hidden)',
            'input "Write a comment"',
            'input "Bio" = "Hello there"',
        ]

        assert not isinstance(await type_into("Notes", "Moved to 4"), Rejection)
        assert not isinstance(await type_into("Write a comment", "Looks good"), Rejection)
        page = browser._page
        assert await page.input_value("#notes") == "Moved to 4"
        assert await page.input_value("textarea[placeholder]") == "Looks good"
    finally:
        await isolated_runtime.shutdown()


def test_textarea_is_resolved_by_label_and_placeholder_and_hides_a_secret():
    notes = make_element(0, tag="textarea", value="Meeting at 3")
    notes.aria_label = "Notes"
    secret = make_element(1, tag="textarea", name="api_key", value="sk-live-0123456789abcdef")
    comment = make_element(2, tag="textarea", placeholder="Write a comment")
    elements = [notes, secret, comment]

    assert resolve_element("Notes", elements, kind_filter="input").found is notes
    assert resolve_element("Write a comment", elements, kind_filter="input").found is comment
    assert describe(secret) == 'input "api_key" = (value hidden)'
    assert describe(notes) == 'input "Notes" = "Meeting at 3"'
