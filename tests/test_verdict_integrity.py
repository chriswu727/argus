"""A receipt must not certify a symptom from evidence that cannot show it."""

from __future__ import annotations

import http.server
import json
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import pytest

from argus.adapters.reporting.common import _repro_badge, xml_safe
from argus.adapters.reporting.html import render_html
from argus.adapters.reporting.junit import render_junit
from argus.adapters.reporting.sarif import render_sarif
from argus.application.lifecycle import start_session
from argus.application.probes import verify_persistence
from argus.application.regression import RegressionRun, check_entries
from argus.application.text import _token_present
from argus.application.verification import (
    _moved_from,
    _run_reproduction_check,
    _status_observation,
)
from argus.cli import _run_regression, _write_regression_artifact
from argus.domain.models import Bug, BugType, ExplorationResult, Severity
from argus.domain.verification import receipt_outcome

LONG_PAGE = (
    "<h1>Inventory</h1><table>"
    + "".join(f"<tr><td>Item-{i:04d}</td><td>row {i} description</td></tr>" for i in range(400))
    + "</table><ul><li>first A</li><li>first B</li></ul><ul><li>second A</li><li>second B</li></ul>"
)

HASH_APP = """<h1>Profile</h1><p id="out"></p>
<button onclick="window.saved='UNSAVED-NAME';render()">Save</button>
<script>function render(){document.getElementById('out').textContent=window.saved||''}
window.onhashchange=render;render()</script>"""


@pytest.fixture
def app():
    class Handler(http.server.BaseHTTPRequestHandler):
        pages: dict = {}

        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body, location = self.pages.get(
                urlparse(self.path).path, (200, "<p>home</p>", None)
            )
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

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


def _entry(**verify):
    return {
        "fingerprint": "f",
        "title": "finding",
        "severity": "high",
        "verify": {"version": 1, "login_page": False, "clear_storage": False, **verify},
    }


async def test_text_far_down_a_long_page_is_found(isolated_runtime, app):
    pages, base = app
    pages["/"] = (200, LONG_PAGE, None)
    try:
        await start_session(base)
        state = await isolated_runtime.session.browser.get_state()
        assert len(state.page_text) == 5000 and "Item-0390" not in state.page_text
        assert "Item-0390" in state.full_text and not state.text_truncated
        assert len(state.item_lists) == 2

        absent = await _run_reproduction_check(
            isolated_runtime.session, {"expect": "absent", "target_text": "Item-0390"}
        )
        assert absent["reproduced"] is False, absent
        present = await _run_reproduction_check(
            isolated_runtime.session, {"expect": "present", "target_text": "Item-0390"}
        )
        assert present["reproduced"] is True, present
        checked = await verify_persistence("present", "Item-0390")
        assert checked.data["verification"]["matches"] is True
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize(
    "needle, haystack, found",
    [
        ("已完成", "任务已完成", True),
        ("ログイン", "ここでログインする", True),
        ("cat", "category", False),
        ("Item-039", "Item-0390", False),
        ("-50%", "$89.99-50%", True),
    ],
)
def test_token_boundaries_follow_the_script(needle, haystack, found):
    assert _token_present(needle, haystack) is found


async def test_hash_route_check_reloads_the_document(isolated_runtime, app):
    pages, base = app
    pages["/"] = (200, HASH_APP, None)
    try:
        await start_session(base + "/#/profile")
        await isolated_runtime.session.browser._page.click("button")
        receipt = await _run_reproduction_check(
            isolated_runtime.session, {"expect": "present", "target_text": "UNSAVED-NAME"}
        )
        assert receipt["reproduced"] is False, receipt

        await isolated_runtime.session.browser._page.click("button")
        checked = await verify_persistence("present", "UNSAVED-NAME")
        assert checked.data["verification"]["present"] is False
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize(
    "expected, direct, final, moved, login, outcome",
    [
        (500, 500, 500, False, False, True),
        (500, 200, 200, False, False, False),
        (500, 401, 401, False, False, None),
        (500, 503, 503, False, False, None),
        (500, 302, 200, True, True, None),
        (500, 200, 200, False, True, None),
        (200, 200, 200, False, False, True),
        (200, 302, 200, True, False, None),
        (200, 200, 200, False, True, None),
        (200, 401, 401, False, False, None),
        (200, 404, 404, False, False, False),
        (404, None, None, False, False, None),
    ],
)
def test_status_mismatch_is_fixed_only_on_a_clean_answer(
    expected, direct, final, moved, login, outcome
):
    matched, reason = _status_observation(expected, direct, final, moved, login)
    assert matched is outcome
    assert (reason is None) is (outcome is not None)


@pytest.mark.parametrize(
    "page",
    [
        (401, "<h1>Unauthorized</h1>", None),
        (503, "<h1>Deploying</h1>", None),
        (302, "", "/login"),
    ],
)
async def test_recorded_error_status_is_not_fixed_by_another_failure(isolated_runtime, app, page):
    pages, base = app
    pages["/report"] = page
    pages["/login"] = (200, '<h1>Welcome back</h1><input type="password"><button>Go</button>', None)
    try:
        await start_session(base)
        results = await check_entries(
            isolated_runtime.session,
            [_entry(kind="http_status", expect_status=500, at_url=base + "/report")],
            base,
        )
        assert results[0]["status"] == "INCONCLUSIVE", results
        assert RegressionRun(results).exit_code == 3

        pages["/report"] = (200, "<h1>Report</h1>", None)
        fixed = await check_entries(
            isolated_runtime.session,
            [_entry(kind="http_status", expect_status=500, at_url=base + "/report")],
            base,
        )
        assert fixed[0]["status"] == "FIXED", fixed
    finally:
        await isolated_runtime.shutdown()


async def test_redirect_does_not_verify_a_success_status(isolated_runtime, app):
    pages, base = app
    pages["/admin"] = (302, "", "/login")
    pages["/login"] = (200, "<h1>Members</h1>", None)
    try:
        await start_session(base)
        receipt = await _run_reproduction_check(
            isolated_runtime.session, {"expect_status": 200, "at_url": base + "/admin"}
        )
        assert receipt["reproduced"] is None, receipt
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize(
    "login, redirect",
    [
        ('<h1>Welcome back</h1><input type="password"><button>Continue</button>', True),
        ('<h1>Anmelden</h1><input type="password"><button>Weiter</button>', False),
        ('<h1>Welcome back</h1><input type="password"><button>Sign in</button>', False),
        ("<h1>Pick a workspace</h1>", True),
    ],
)
async def test_absence_behind_a_login_or_redirect_is_inconclusive(
    isolated_runtime, app, login, redirect
):
    pages, base = app
    pages["/cart"] = (302, "", "/login") if redirect else (200, login, None)
    pages["/login"] = (200, login, None)
    try:
        await start_session(base)
        results = await check_entries(
            isolated_runtime.session,
            [
                _entry(
                    kind="text", expect="present", target_text="Total: $-5", at_url=base + "/cart"
                )
            ],
            base,
        )
        assert results[0]["status"] == "INCONCLUSIVE", results
    finally:
        await isolated_runtime.shutdown()


def test_moved_from_compares_pages_not_anchors():
    assert _moved_from("http://a.test/cart", "http://a.test/login?next=/cart")
    assert _moved_from("http://a.test/#/tasks", "http://a.test/#/login")
    assert not _moved_from("http://a.test/cart", "http://a.test/cart/")
    assert not _moved_from("http://a.test/", "http://a.test/#section")
    assert not _moved_from("http://a.test/x?page=2", "http://a.test/x")


@pytest.mark.parametrize("url", ["127.0.0.1:3000", "localhost", "http://u:p@example.test/", ""])
async def test_regression_rejects_a_url_that_selects_no_journal(tmp_path, url):
    path = tmp_path / ".argus" / "journal" / "default.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "entries": []}))
    assert await _run_regression(url, str(tmp_path)) == 2


@pytest.mark.parametrize("field, value", [("title", 123), ("severity", None)])
async def test_journal_entry_with_a_wrong_type_is_a_storage_error(tmp_path, field, value):
    path = tmp_path / ".argus" / "journal" / "example.test.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "entries": [{**_entry(), field: value}]}))
    assert await _run_regression("http://example.test/", str(tmp_path)) == 2


async def test_regression_names_origins_that_do_have_journals(tmp_path, capsys):
    path = tmp_path / ".argus" / "journal" / "127.0.0.1_3000.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "entries": [_entry()]}))
    assert await _run_regression("http://localhost:3000/", str(tmp_path)) == 0
    assert "127.0.0.1_3000" in capsys.readouterr().out.replace("\n", "")


def test_regression_artifacts_are_redacted_and_well_formed(tmp_path):
    results = [
        {
            "title": "Console \x1b[31mTypeError\x00",
            "severity": "high",
            "status": "STILL-PRESENT",
            "runs": "2/2",
            "reason": None,
            "url": "http://example.test/reset?access_token=SECRETTOKEN123",
        }
    ]
    artifact = _write_regression_artifact(str(tmp_path), "http://example.test/", results)
    text = Path(artifact + ".json").read_text() + Path(artifact + ".junit.xml").read_text()
    assert "SECRETTOKEN123" not in text
    assert ET.parse(artifact + ".junit.xml").find("testcase/failure") is not None


def _result(receipt, severity=Severity.HIGH, title="Finding"):
    bug = Bug(
        type=BugType.UX_ISSUE,
        severity=severity,
        title=title,
        description="d",
        url="http://example.test/",
        steps_to_reproduce=[],
        reproduction_receipt=receipt,
    )
    return ExplorationResult(
        url="http://example.test/",
        bugs=[bug],
        pages_visited=[],
        actions_taken=0,
        duration_seconds=1.0,
        focus_areas=[],
        run_id="run1",
    )


def test_intermittent_receipt_is_inconclusive_everywhere():
    flaky = {"attempted": True, "reproduced": False, "flaky": True, "runs": "1/2"}
    assert receipt_outcome(flaky) is None
    assert receipt_outcome({"reproduced": False}) is False
    assert receipt_outcome(None) is None

    suite = ET.fromstring(render_junit(_result(flaky)))
    assert suite.get("failures") == "1"
    assert suite.find("testcase").get("name").startswith("[INTERMITTENT]")
    assert suite.find("testcase/failure") is not None

    properties = json.loads(render_sarif(_result(flaky)))["runs"][0]["results"][0]["properties"]
    assert properties["reproduced"] is None and properties["intermittent"] is True
    assert "INTERMITTENT" in _repro_badge({**flaky, "expect_status": 500})


@pytest.mark.parametrize(
    "severity, level",
    [(Severity.CRITICAL, "error"), (Severity.HIGH, "error"), (Severity.INFO, "note")],
)
def test_sarif_level_follows_severity(severity, level):
    doc = json.loads(render_sarif(_result({"reproduced": True}, severity)))
    assert doc["runs"][0]["results"][0]["level"] == level


def test_junit_survives_control_characters():
    xml = render_junit(_result({"reproduced": True}, title="Console \x1b[31mTypeError\x00"))
    assert ET.fromstring(xml).find("testcase/failure") is not None
    assert xml_safe("a\x00b\x1bc\td\ne") == "abc\td\ne"


def test_html_shows_an_inconclusive_check_as_inconclusive():
    result = _result({"reproduced": True})
    result.coverage = {
        "goals": [
            {
                "goal": "Save profile",
                "status": "exercised",
                "evidence_refs": {
                    "verifications": [
                        {
                            "expect": "absent",
                            "target_text": "Old name",
                            "url": "/p",
                            "matches": None,
                        }
                    ]
                },
            }
        ]
    }
    html = render_html(result, report_dir=Path("."), portable=True, asset_dir=None)
    assert "INCONCLUSIVE</span> absent Old name" in html
    assert "MISMATCH</span> absent Old name" not in html
