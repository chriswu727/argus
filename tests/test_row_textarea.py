"""A <textarea>'s content is a value: it is not part of its row's text."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from argus.application.actions import click_what
from argus.application.exports import export_test
from argus.application.findings import record_bug
from argus.application.lifecycle import start_session
from argus.application.observation import observe
from argus.resolver import describe

SECRET = "tok-secret-999"


@pytest.fixture
def row_app():
    class Handler(http.server.BaseHTTPRequestHandler):
        rows = [("Alpha", "tok-secret-111"), ("Beta", SECRET)]
        posts: list = []

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = (
                "<h1>Tokens</h1><p>SAVE-BUG</p><ul>"
                + "".join(
                    f"<li><span>{name}</span> <textarea name='token'>{content}</textarea>"
                    f" <button onclick=\"fetch('/save?row={name}',{{method:'POST'}})\">Save</button></li>"
                    for name, content in self.rows
                )
                + "</ul>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            self.posts.append(parse_qs(urlparse(self.path).query)["row"][0])
            self.send_response(200)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Handler, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_textarea_content_stays_out_of_row_context(isolated_runtime, row_app):
    _, base = row_app
    try:
        await start_session(base + "/")
        elements = await isolated_runtime.session.browser.get_elements()
        in_rows = [el for el in elements if el.tag in ("textarea", "button")]
        rows = [" ".join((el.parent_context or "").split()) for el in in_rows]
        assert rows == ["Alpha Save"] * 2 + ["Beta Save"] * 2
        token = next(el for el in in_rows if el.tag == "textarea" and el.value == SECRET)
        assert describe(token) == "input \"token\" = (value hidden) (near: 'Beta Save')"
        observation = await observe()
        assert "(value hidden)" in observation
        assert not [line for line in str(observation).splitlines() if SECRET in line]
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("change", ["edited", "indistinct"])
async def test_exported_row_ignores_textarea_content(isolated_runtime, row_app, change):
    handler, base = row_app
    try:
        await start_session(base + "/")
        await click_what("Save near Beta")
        assert handler.posts == ["Beta"]
        recorded = await record_bug(
            "Save symptom",
            "high",
            {"screenshot": "skip"},
            verify={"expect": "present", "target_text": "SAVE-BUG"},
        )
        handler.rows = {
            "edited": [("Alpha", "changed-1"), ("Beta", "changed-2")],
            "indistinct": [("Beta", "tok-secret-111"), ("Beta", SECRET)],
        }[change]
        handler.posts.clear()
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert SECRET not in Path(exported.data["path"]).read_text()
        if change == "edited":
            assert handler.posts == ["Beta"], exported
            assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
        else:
            assert handler.posts == [], exported
            assert exported.data["run"]["outcome"] == "errored", exported
            assert "missing or ambiguous" in str(exported.data["run"]), exported
    finally:
        await isolated_runtime.shutdown()
