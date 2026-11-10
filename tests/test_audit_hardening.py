"""Regressions for defects found in the 0.12.2 audit."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from argus.access import AccessPolicy
from argus.application.evidence import _auto_screenshot
from argus.application.lifecycle import start_session
from argus.application.navigation import navigate
from argus.cli import cleanup
from argus.security import _redact, is_secret_key, redacted_copy


@pytest.fixture
def site():
    class Handler(http.server.BaseHTTPRequestHandler):
        pages: dict = {}

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.pages.get(self.path, "<p>home</p>").encode())

    Handler.pages = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Handler.pages, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cleanup_ignores_a_report_name_that_is_not_a_run_id(tmp_path):
    root = tmp_path / "argus-reports"
    journal = root / ".argus" / "journal" / "example.test.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}")
    (root / "screenshots" / "run1").mkdir(parents=True)
    (root / "report_run1.html").write_text("report")
    (root / "report_...html").write_text("x")

    result = CliRunner().invoke(cleanup, ["--output", str(root), "--keep-runs", "0", "--apply"])

    assert result.exit_code == 0, result.output
    assert journal.exists()
    assert (root / "report_...html").exists()
    assert not (root / "report_run1.html").exists()


def test_redaction_is_linear_on_a_long_token_free_body():
    started = time.perf_counter()
    _redact("a" * 400_000)
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize(
    "text",
    [
        "user=alice&pwd=SYNTHETIC_SECRET",
        'login failed for password="SYNTHETIC_SECRET"',
        "Authorization: Bearer abc123+SYNTHETIC_SECRET/x==",
        "curl -H 'Authorization: Basic SYNTHETIC_SECRET' http://x",
        '{"private_key": "SYNTHETIC_SECRET"',
        "https://s3.test/o?X-Amz-Signature=SYNTHETIC_SECRET",
    ],
)
def test_common_credential_shapes_are_redacted(text):
    assert "SYNTHETIC_SECRET" not in _redact(text)


def test_ordinary_keys_that_contain_a_short_secret_word_are_kept():
    data = {"author": "Jane", "sidebar": "open", "inside": True, "president": "x"}
    assert redacted_copy(data) == data
    assert _redact("https://shop.test/list?side=left&author=jane") == (
        "https://shop.test/list?side=left&author=jane"
    )
    for key in ("JSESSIONID", "PHPSESSID", "connect.sid", "csrfmiddlewaretoken", "userPwd"):
        assert is_secret_key(key), key


def test_browser_error_page_is_not_a_policy_violation():
    AccessPolicy(allowed_origins=("http://app.test",)).check_url("chrome-error://chromewebdata/")


async def test_auto_screenshot_name_cannot_leave_the_run_directory(isolated_runtime, site):
    _, base = site
    try:
        await start_session(base)
        path = Path(
            await _auto_screenshot(isolated_runtime.session, "action_../../../../ESC", "step")
        )
        assert path.parent == isolated_runtime.settings.output_dir / "screenshots" / (
            isolated_runtime.session.run_id
        )
    finally:
        await isolated_runtime.shutdown()


async def test_form_with_a_control_named_id_is_observable(isolated_runtime, site):
    pages, base = site
    pages["/"] = (
        '<form><input type="hidden" name="id" value="42"><label>Title <input name="title"></label>'
        '<input type="text"><input type="submit" value="Save"></form>'
    )
    try:
        await start_session(base)
        state = await isolated_runtime.session.browser.get_state()
        assert any(el.name == "title" for el in state.elements)
    finally:
        await isolated_runtime.shutdown()


async def test_each_page_event_is_captured_once(isolated_runtime, site):
    pages, base = site
    pages["/"] = "<script>console.error('boom once')</script><p>home</p>"
    try:
        await start_session(base)
        browser = isolated_runtime.session.browser
        documents = [
            entry
            for entry in browser.network_log_snapshot()
            if entry["resource_type"] == "document"
        ]
        assert len(documents) == 1 and documents[0]["status"] == 200
        assert [e for e in browser.console_errors if "boom once" in str(e)].__len__() == 1
    finally:
        await isolated_runtime.shutdown()


async def test_session_recovers_after_a_failed_navigation(isolated_runtime, site):
    _, base = site
    try:
        await start_session(base)
        try:
            await isolated_runtime.session.browser.goto("http://127.0.0.1:9/")
        except Exception:
            pass
        result = await isolated_runtime.execute("navigate", navigate, base + "/")
        assert result.error is None, result.value
    finally:
        await isolated_runtime.shutdown()


async def test_unsaved_changes_prompt_does_not_block_navigation(isolated_runtime, site):
    pages, base = site
    pages["/"] = (
        "<input id='f'><script>addEventListener('beforeunload',"
        "e=>{e.preventDefault();e.returnValue='x'})</script>"
    )
    pages["/next"] = "<p>NEXT-PAGE</p>"
    try:
        await start_session(base)
        browser = isolated_runtime.session.browser
        await browser._page.fill("#f", "draft")
        await browser.goto(base + "/next")
        assert browser.current_url.endswith("/next")
    finally:
        await isolated_runtime.shutdown()


async def test_internal_links_are_judged_by_origin_not_prefix(isolated_runtime, site):
    pages, base = site
    pages["/"] = f'<a href="{base}0/x">other port</a><a href="/in">inside</a>'
    try:
        await start_session(base)
        links = (await isolated_runtime.session.browser.get_state()).links
        assert {link["text"]: link["isInternal"] for link in links} == {
            "other port": False,
            "inside": True,
        }
    finally:
        await isolated_runtime.shutdown()


async def test_rejected_start_and_empty_end_leave_no_stuck_state(isolated_runtime):
    from argus.application.manager import SessionState
    from argus.domain.contracts import ErrorCode, OperationError

    async def start_session():
        isolated_runtime.transition(SessionState.STARTING)
        raise OperationError(ErrorCode.PERMISSION, "outside the allowed origins")

    async def end_session():
        return "No active session."

    rejected = await isolated_runtime.execute("start_session", start_session)
    assert rejected.error is ErrorCode.PERMISSION
    assert isolated_runtime.state is SessionState.FAILED
    await isolated_runtime.execute("end_session", end_session)
    assert isolated_runtime.state is SessionState.IDLE


async def test_action_evidence_survives_compact_mode(isolated_runtime, site):
    from argus.application.journeys import test_action as run_action

    pages, base = site
    pages["/"] = (
        "<button onclick=\"fetch('/missing-api').then(()=>console.error('save failed'))\">Save</button>"
    )
    try:
        await start_session(base)
        result = await run_action("Save")
        events = result.data["browser_events"]
        assert any("save failed" in text for text in events["console"]) or events["http"]
        assert "cross_stack" in result.data
    finally:
        await isolated_runtime.shutdown()


async def test_failed_action_is_an_error_not_a_completed_call(isolated_runtime, site):
    from argus.application.actions import upload_file
    from argus.application.screenshots import screenshot_diff
    from argus.domain.contracts import ErrorCode

    _, base = site
    try:
        await start_session(base)
        empty = await isolated_runtime.execute("upload_file", upload_file, "Attach", [])
        assert empty.error is ErrorCode.INVALID_ARGUMENT
        missing = await isolated_runtime.execute(
            "screenshot_diff", screenshot_diff, "nope-a", "nope-b"
        )
        assert missing.error is not None
    finally:
        await isolated_runtime.shutdown()


def test_coverage_goal_matches_whole_words_only(isolated_runtime):
    from argus.application.coverage import _coverage_goal_index

    session = isolated_runtime.session
    session.coverage_goals = [
        {"goal": "Checkout with credit card"},
        {"goal": "Delete account"},
    ]
    assert _coverage_goal_index(session, "edit")[0] is None
    assert _coverage_goal_index(session, "count")[0] is None
    assert _coverage_goal_index(session, "delete account")[0] == 1
    assert _coverage_goal_index(session, "credit card")[0] == 0


async def test_replaced_mock_does_not_survive_suspension(isolated_runtime, site):
    pages, base = site
    pages["/api"] = "REAL"
    try:
        await start_session(base)
        browser = isolated_runtime.session.browser
        await browser.add_route("**/api", status=500, body="FIRST")
        await browser.add_route("**/api", status=201, body="SECOND")

        async def fetched():
            return await browser._page.evaluate("fetch('/api').then(r => r.text())")

        assert await fetched() == "SECOND"
        suspended = await browser.suspend_mocks()
        assert await fetched() == "REAL"
        await browser.restore_mocks(suspended)
        assert await fetched() == "SECOND"
        assert await browser.remove_route("**/api")
        assert await fetched() == "REAL"
    finally:
        await isolated_runtime.shutdown()
