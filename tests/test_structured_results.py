"""A host that reads only structuredContent gets every result and every reason.

Claude Code shows the model the structured envelope instead of the text
blocks when both are present. A tool that answered in plain text therefore
reached it as `data: {}`: a real host called regression_check, saw nothing,
and tested again by hand. These tests read `structuredContent` only.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from argus.application.progress import progress_scope
from argus.domain.contracts import ErrorCode, ImageArtifact, OperationResult, ToolText
from argus.entrypoints.mcp.results import to_mcp_result

SECRET = "SECRETVALUE42"


class _Site(http.server.BaseHTTPRequestHandler):
    state = "buggy"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = "<h1>Task list</h1><button>Add task</button>"
        if type(self).state == "buggy":
            body += "<p>GHOST-BUG</p>"
        self.send_response(503 if type(self).state == "unavailable" else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())


@pytest.fixture
def site():
    class Site(_Site):
        state = "buggy"

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Site, f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def _data(result):
    return (result.structuredContent or {}).get("data", {})


async def test_results_and_reasons_reach_a_structured_only_host(site, tmp_path):
    handler, url = site
    env = dict(os.environ, ARGUS_OUTPUT_DIR=str(tmp_path / "reports"))
    server = StdioServerParameters(command=sys.executable, args=["-m", "argus.mcp_server"], env=env)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments):
                return await client.call_tool(name, arguments)

            started = await call("start_session", {"url": url})
            assert not started.isError, started.content

            empty = _data(await call("regression_check", {}))
            assert empty["reason"] == "no_journal" and empty["checked"] == 0
            assert empty["results"] == [] and "journaled" in empty["detail"]

            invalid = await call(
                "verify_persistence", {"expect": "maybe", "target_text": "GHOST-BUG"}
            )
            assert invalid.isError
            assert invalid.structuredContent["error_code"] == "invalid_argument"
            assert "expect" in _data(invalid)["message"]

            errors = _data(await call("get_errors", {}))
            assert "No new console or network events" in errors["message"]

            missing = await call("capsule_restore", {"name": "missing-fixture"})
            assert missing.isError
            assert missing.structuredContent["error_code"] == "invalid_argument"
            assert "no capsule named 'missing-fixture'" in _data(missing)["message"]

            # The finding's page carries a token in its query; no result may show it.
            at_url = f"{url}?session_token={SECRET}"
            recorded = _data(
                await call(
                    "record_bug",
                    {
                        "title": "Ghost task remains",
                        "severity": "high",
                        "evidence": {"screenshot": "skip"},
                        "verify": {
                            "expect": "present",
                            "target_text": "GHOST-BUG",
                            "at_url": at_url,
                        },
                    },
                )
            )
            assert recorded["verdict"] == "verified"
            assert not (await call("end_session", {})).isError

            assert not (
                await call("start_session", {"url": url, "review_mode": "regression"})
            ).isError
            for state, status, key in (
                ("buggy", "STILL-PRESENT", "still_present"),
                ("fixed", "NO-LONGER-REPRODUCES", "no_longer_reproduces"),
                ("unavailable", "INCONCLUSIVE", "inconclusive"),
            ):
                handler.state = state
                result = await call("regression_check", {})
                data = _data(result)
                assert not result.isError
                assert data["checked"] == 1 and data["counts"][key] == 1, (state, data)
                (finding,) = data["results"]
                assert finding["status"] == status and finding["title"] == "Ghost task remains"
                assert finding["path"] == "/"
                if state == "unavailable":
                    assert "503" in finding["reason"]
                else:
                    assert finding["runs"] in ("2/2", "0/2")
                assert SECRET not in json.dumps(result.structuredContent)
                assert "@" not in data["site"]
            assert not (await call("end_session", {})).isError


async def test_regression_check_outside_a_web_session_is_an_error(isolated_runtime):
    from argus.application.regression import regression_check

    isolated_runtime.session.mode = "screen"
    isolated_runtime.session.screen = object()
    result = await isolated_runtime.execute("regression_check", regression_check)
    assert result.error == ErrorCode.INVALID_ARGUMENT
    assert "web-mode only" in str(result.value)


def _encode(value, error=None):
    with progress_scope() as progress:
        return to_mcp_result("probe", OperationResult(value, error), progress)


def test_text_only_results_carry_a_redacted_message():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlc2lnbmF0dXJl"
    completed = _encode(f"Scrolled down. Authorization: Bearer {token}")
    message = completed.structuredContent["data"]["message"]
    assert message.startswith("Scrolled down.") and token not in message
    # The text blocks are unchanged for hosts that read them.
    assert token in completed.content[0].text

    failed = _encode("click_what: no element matches 'Save'", ErrorCode.INVALID_ARGUMENT)
    assert failed.isError
    assert failed.structuredContent["data"] == {"message": "click_what: no element matches 'Save'"}


def test_structured_results_are_not_given_a_second_copy(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"")
    structured = _encode(ToolText("Long text", {"verdict": "verified"}))
    assert structured.structuredContent["data"] == {"verdict": "verified"}
    shot = _encode([f"Screenshot saved: {image}", ImageArtifact(path=image)])
    data = shot.structuredContent["data"]
    assert data["message"] == f"Screenshot saved: {image}"
    assert data["artifacts"] == [{"kind": "image", "path": str(image)}]
    # A ToolText that carries no fields of its own still gives its text.
    assert _encode(ToolText("x", {})).structuredContent["data"] == {"message": "x"}
