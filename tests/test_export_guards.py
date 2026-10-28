from __future__ import annotations

import http.server
import threading

import pytest

from argus.application.testgen import generate_test, locator_expr, run_exported_test
from argus.domain.models import InteractiveElement


@pytest.fixture
def export_app():
    class Handler(http.server.BaseHTTPRequestHandler):
        mode = "buggy"
        right_clicks = 0
        wrong_clicks = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            status = 200
            content_type = "text/html"
            if self.path == "/diagram.svg":
                content_type = "image/svg+xml"
                body = '<svg xmlns="http://www.w3.org/2000/svg"><text>Decoration</text></svg>'
            elif self.path == "/wrong":
                Handler.wrong_clicks += 1
                body = "ok"
            elif self.path == "/right":
                Handler.right_clicks += 1
                body = "ok"
            elif self.path == "/outer":
                body = '<iframe id="inner" src="/frame"></iframe>'
            elif self.path == "/frame":
                body = (
                    "<p>Frame symptom</p>"
                    '<button id="delete" onclick="fetch(\'/right\')">Delete</button>'
                    "<ul><li>Other task<button>Remove</button></li>"
                    "<li>Target task<button onclick=\"fetch('/right')\">Remove</button></li></ul>"
                )
            elif Handler.mode in ("404", "500"):
                status = int(Handler.mode)
                body = "Application unavailable"
            elif Handler.mode == "redirect" and self.path != "/login":
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            elif self.path == "/login" or Handler.mode == "login":
                body = "Please sign in to continue"
            elif Handler.mode == "login-form":
                body = '<h1>Sign in</h1><input type="password"><button>Log in</button>'
            elif Handler.mode == "svg":
                body = '<p>The task stays deleted</p><iframe src="/diagram.svg"></iframe>'
            elif Handler.mode.startswith("frame"):
                src = "/outer" if Handler.mode == "frame-nested" else "/frame"
                body = (
                    '<button id="delete" onclick="fetch(\'/wrong\')">Delete</button>'
                    "<ul><li>Target task<button onclick=\"fetch('/wrong')\">Remove</button></li></ul>"
                    f'<iframe id="editor" src="{src}"></iframe>'
                )
                if not Handler.right_clicks:
                    body += "Wrong control was used"
            elif Handler.mode == "fixed":
                body = "The task stays deleted"
            elif Handler.mode == "account":
                body = "Account settings: login notifications"
            else:
                body = "Deleted task comes back"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body.encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Handler, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _export(tmp_path, base, *, journey=None, target="Deleted task comes back", expect="present"):
    path = tmp_path / "regression.py"
    source, notes = generate_test(
        finding_id="run:1",
        title="Deleted task comes back",
        expected="The task stays deleted",
        receipt={"expect": expect, "target_text": target, "at_url": base + "/check"},
        journey=journey or [],
        start_url=base + "/",
        resolve_input=lambda action: None,
        filename=path.name,
    )
    path.write_text(source)
    return path, notes


@pytest.mark.parametrize("mode", ["404", "500", "login", "login-form", "redirect"])
@pytest.mark.parametrize("expect", ["present", "absent"])
async def test_exported_checks_reject_error_pages_and_login_walls(
    tmp_path, export_app, mode, expect
):
    handler, base = export_app
    handler.mode = mode
    path, _ = _export(tmp_path, base, expect=expect)
    result = await run_exported_test(path, 30)
    assert result["outcome"] == "errored", result
    assert "fresh-load check" in result["detail"]


@pytest.mark.parametrize(
    "mode,outcome", [("buggy", "fails_on_symptom"), ("fixed", "passes"), ("account", "passes")]
)
async def test_exported_checks_keep_valid_page_verdicts(tmp_path, export_app, mode, outcome):
    handler, base = export_app
    handler.mode = mode
    path, _ = _export(tmp_path, base)
    assert (await run_exported_test(path, 30))["outcome"] == outcome


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("by_id", [False, True])
async def test_exported_actions_keep_frame_scope_and_row_identity(
    tmp_path, export_app, nested, by_id
):
    handler, base = export_app
    handler.mode = "frame-nested" if nested else "frame"
    frame = 'iframe[id="editor"]' + (' >>> iframe[id="inner"]' if nested else "")
    target = InteractiveElement(
        index=2, tag="button", text="Delete" if by_id else "Remove", frame=frame
    )
    other = InteractiveElement(index=1, tag="button", text=target.text, frame=frame)
    outside = InteractiveElement(index=0, tag="button", text=target.text)
    if by_id:
        target.id = outside.id = "delete"
    else:
        target.parent_context = outside.parent_context = "Target task"
        other.parent_context = "Other task"
    locator = locator_expr(target, [outside, other, target])
    path, notes = _export(
        tmp_path,
        base,
        journey=[{"tool": "click_what", "locator": locator}],
        target="Wrong control was used",
    )
    assert notes == []
    result = await run_exported_test(path, 30)
    assert result["outcome"] == "passes", result
    assert handler.right_clicks == 1 and handler.wrong_clicks == 0


async def test_exported_text_checks_include_frame_content(tmp_path, export_app):
    handler, base = export_app
    handler.mode = "frame-nested"
    path, _ = _export(tmp_path, base, target="Frame symptom")
    assert (await run_exported_test(path, 30))["outcome"] == "fails_on_symptom"


async def test_exported_text_checks_ignore_bodyless_embeds(tmp_path, export_app):
    handler, base = export_app
    handler.mode = "svg"
    path, _ = _export(tmp_path, base)
    assert (await run_exported_test(path, 10))["outcome"] == "passes"
