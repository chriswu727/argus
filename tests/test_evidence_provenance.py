from __future__ import annotations

import hashlib
import http.server
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import argus.mcp_server as m
from argus.adapters.reporting.html import render_html
from argus.adapters.reporting.json import render_json
from argus.adapters.reporting.junit import render_junit
from argus.adapters.reporting.sarif import render_sarif
from argus.application.coverage import _apply_coverage_updates
from argus.application.evidence import _record_coverage_action
from argus.application.findings import record_bug
from argus.domain.contracts import Rejection
from argus.domain.models import Bug, BugType, ExplorationResult, Screenshot, Severity


@pytest.fixture
def evidence_site():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(("GET", self.path))
            pages = {
                "/profile": '<h1>Profile</h1><p>Alex</p><form method="post"><label>Display name<input name="name"></label><button>Save</button></form>',
                "/checkout": "<h1>Checkout</h1><p>Subtotal: $12</p>",
                "/tasks": "<h1>Tasks</h1><p>No tasks</p>",
                "/login": "<h1>Please log in</h1>",
            }
            self.send_response(200 if self.path in pages else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body>{pages.get(self.path, 'Not found')}</body></html>".encode()
            )

        def do_POST(self):
            requests.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Location", "/profile")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_deferred_findings_capture_verified_pages_without_repeating_writes(
    isolated_runtime, evidence_site
):
    origin, requests = evidence_site
    await m.start_session(origin + "/profile", goals=["profile", "checkout"])
    try:
        await m.test_form({"Display name": "Morgan"}, "Save")
        await m.navigate(origin + "/tasks")
        for title, goal, target, expect in (
            ("Saved name is lost", "profile", "Morgan", "absent"),
            ("Checkout ignores quantity", "checkout", "Subtotal: $12", "present"),
        ):
            result = await record_bug(
                title,
                "high",
                {"goal": goal, "expected": "Required state", "actual": "Incorrect state"},
                {"at_url": "/" + goal, "target_text": target, "expect": expect},
            )
            assert result.data["verdict"] == "verified"
            assert result.data["finding"]["url"] == origin + "/" + goal
            assert not result.data["warnings"]
            assert isolated_runtime.session.browser.current_url == origin + "/tasks"
        session = isolated_runtime.session
        bug_paths = {bug.screenshot_path for bug in session.bugs}
        assert [shot.url for shot in session.screenshots if shot.path in bug_paths] == [
            origin + "/profile",
            origin + "/checkout",
        ]
        hashes = [
            hashlib.sha256(Path(b.screenshot_path).read_bytes()).hexdigest() for b in session.bugs
        ]
        assert len(set(hashes)) == 2
        assert sum(method == "POST" for method, _ in requests) == 1
        assert [path for method, path in requests if method == "GET"].count("/checkout") == 2
    finally:
        await m.end_session()


@pytest.mark.parametrize("path", ["/login", "/missing"])
async def test_inconclusive_evidence_keeps_actual_page_and_restores_caller(
    isolated_runtime, evidence_site, path
):
    origin, _ = evidence_site
    await m.start_session(origin + "/tasks")
    try:
        result = await record_bug(
            "State was lost",
            "high",
            verify={"at_url": path, "expect": "absent", "target_text": "Morgan"},
        )
        assert result.data["verdict"] == "inconclusive"
        assert result.data["finding"]["url"] == origin + path
        assert isolated_runtime.session.screenshots[-1].url == origin + path
        assert isolated_runtime.session.browser.current_url == origin + "/tasks"
    finally:
        await m.end_session()


@pytest.mark.parametrize("directive", ["skip", "auto", "label"])
async def test_screenshot_skip_and_failure_do_not_change_verification(
    isolated_runtime, evidence_site, directive, monkeypatch
):
    origin, _ = evidence_site
    await m.start_session(origin + "/tasks")
    try:
        screenshot = AsyncMock(side_effect=OSError("Synthetic unavailable disk"))
        monkeypatch.setattr(isolated_runtime.session.browser, "screenshot", screenshot)
        result = await record_bug(
            "Subtotal is incorrect",
            "medium",
            {"screenshot": directive},
            {
                "at_url": "/checkout",
                "expect": "present",
                "target_text": "Subtotal: $12",
                "_screenshot_label": "caller_cannot_override_skip",
            },
        )
        assert result.data["verdict"] == "verified"
        assert result.data["finding"]["screenshot"] is None
        assert screenshot.await_count == (0 if directive == "skip" else 1)
        assert bool(result.data["warnings"]) is (directive != "skip")
        assert isolated_runtime.session.bugs[0].evidence_warnings == result.data["warnings"]
    finally:
        await m.end_session()


@pytest.mark.parametrize(
    "verify", [None, {"at_url": "/checkout", "expect": "present", "target_text": "Subtotal: $12"}]
)
async def test_explicit_mismatched_url_never_attaches_current_page_screenshot(
    isolated_runtime, evidence_site, verify
):
    origin, _ = evidence_site
    await m.start_session(origin + "/tasks")
    try:
        result = await record_bug("Wrong state", "medium", {"url": origin + "/profile"}, verify)
        assert result.data["finding"]["url"] == origin + "/profile"
        assert result.data["finding"]["screenshot"] is None
        assert result.data["warnings"]
        assert not isolated_runtime.session.screenshots
    finally:
        await m.end_session()


def coverage_session(runtime):
    s = runtime.session
    s.mode = "web"
    s.browser = SimpleNamespace(has_page=True, current_url="https://example.test/profile")
    s.coverage_goals = [
        {"goal": goal, "status": "untested"} for goal in ("profile", "newsletter", "return")
    ]
    return s


def update(s, goal, status="in_progress", evidence=""):
    assert (
        _apply_coverage_updates(s, [{"goal": goal, "status": status, "evidence": evidence}]) is None
    )


def test_focus_windows_exclude_later_evidence_but_include_explicit_late_findings(isolated_runtime):
    s = coverage_session(isolated_runtime)
    update(s, "profile")
    _record_coverage_action(s, "test_form", "Save")
    update(s, "newsletter")
    s.browser.current_url = "https://example.test/newsletter"
    _record_coverage_action(s, "test_form", "Subscribe")
    update(s, "return")
    s.browser.current_url = "https://example.test/profile"
    _record_coverage_action(s, "navigate", "Profile")
    s.browser.current_url = "https://example.test/tasks?token=SYNTHETIC_SECRET"
    _record_coverage_action(s, "navigate", "Tasks")
    s.bugs.append(
        Bug(
            BugType.STATE_VERIFICATION,
            Severity.HIGH,
            "Lost name",
            "Evidence",
            "https://example.test/profile",
            [],
            coverage_goals=["profile"],
        )
    )
    update(s, ["profile", "newsletter", "return"], "exercised", "Observed each flow")
    first, second, third = [goal["evidence_refs"] for goal in s.coverage_goals]
    assert [action["description"] for action in first["actions"]] == ["Save"]
    assert [finding["title"] for finding in first["findings"]] == ["Lost name"]
    assert [action["description"] for action in second["actions"]] == ["Subscribe"]
    assert not second["findings"] and "/tasks" not in second["urls"]
    assert third["urls"] == ["/profile", "/tasks"]
    assert not third["findings"]
    assert "SYNTHETIC_SECRET" not in json.dumps(s.coverage_action_trace)


def test_invalid_focus_batch_is_atomic_and_does_not_complete_previous_goals(isolated_runtime):
    s = coverage_session(isolated_runtime)
    update(s, "profile")
    before = deepcopy((s.coverage_goals, s._coverage_goal_cursors, s._coverage_goal_ends))
    assert _apply_coverage_updates(
        s, [{"goal": ["newsletter", "unknown"], "status": "in_progress"}]
    )
    assert (s.coverage_goals, s._coverage_goal_cursors, s._coverage_goal_ends) == before
    update(s, ["newsletter", "return"])
    assert s.coverage_goals[0]["status"] == "in_progress"
    _record_coverage_action(s, "test_action", "Shared journey")
    update(s, ["newsletter", "return"], "exercised", "Observed shared journey")
    assert s.coverage_goals[1]["evidence_refs"] == s.coverage_goals[2]["evidence_refs"]


async def test_late_finding_refreshes_closed_goal_without_polluting_active_goal(isolated_runtime):
    s = coverage_session(isolated_runtime)
    update(s, "profile")
    _record_coverage_action(s, "test_form", "Save")
    update(s, "profile", "exercised", "Saved then reloaded")
    update(s, "newsletter")
    shot_path = isolated_runtime.settings.output_dir / "synthetic.png"
    shot_path.parent.mkdir(parents=True, exist_ok=True)
    shot_path.write_bytes(b"synthetic screenshot")
    s.screenshots.append(Screenshot(str(shot_path), "profile", "Finding", s.browser.current_url))
    result = await record_bug(
        "Lost name", "high", {"goal": "profile", "screenshot": str(shot_path)}
    )
    assert result.data["recorded"]
    profile_refs = s.coverage_goals[0]["evidence_refs"]
    assert profile_refs["findings"][0]["title"] == "Lost name"
    assert profile_refs["screenshots"][0]["path"] == str(shot_path)
    update(s, "newsletter", "exercised", "Checked newsletter")
    assert not s.coverage_goals[1]["evidence_refs"]["findings"]
    assert not s.coverage_goals[1]["evidence_refs"]["screenshots"]


async def test_replay_start_url_is_not_used_as_finding_location(isolated_runtime):
    s = coverage_session(isolated_runtime)
    isolated_runtime.verifiers.verify = AsyncMock(
        return_value={
            "attempted": True,
            "mode": "replay",
            "reproduced": None,
            "at_url": "https://example.test/start",
        }
    )
    result = await record_bug(
        "Journey failure",
        "high",
        {"screenshot": "skip"},
        {"replay": True, "expect": "present", "target_text": "Failure"},
    )
    assert result.data["finding"]["url"] == s.browser.current_url
    assert result.data["verdict"] == "inconclusive"


@pytest.mark.parametrize("render", [render_html, render_json, render_junit, render_sarif])
async def test_expected_actual_and_claimed_references_survive_reports_safely(
    isolated_runtime, render
):
    s = coverage_session(isolated_runtime)
    result = await record_bug(
        "Lost state",
        "high",
        {
            "screenshot": "skip",
            "expected": "Persisted name",
            "actual": "Reset name; token=SYNTHETIC_SECRET",
            "goal": "profile",
            "evidence_calls": [2, 3],
        },
    )
    assert result.data["verdict"] == "inconclusive"
    bug = s.bugs[0]
    assert bug.to_dict()["expected"] == "Persisted name"
    assert bug.to_dict()["actual"].startswith("Reset name")
    assert bug.to_dict()["claimed_evidence_calls"] == [2, 3]
    assert bug.coverage_goals == ["profile"]
    report = render(ExplorationResult(s.browser.current_url, s.bugs, [], 0, 0, []))
    assert "Persisted name" in report and "Reset name" in report
    assert "SYNTHETIC_SECRET" not in report
    assert bug.reproduction_receipt is None


@pytest.mark.parametrize(
    "evidence",
    [
        {"expected": 42},
        {"actual": "x" * 4001},
        {"goal": "missing"},
        {"goals": [42]},
        {"evidence_calls": [True]},
        {"evidence_calls": [0]},
    ],
)
async def test_invalid_finding_metadata_has_no_side_effects(isolated_runtime, evidence):
    s = coverage_session(isolated_runtime)
    assert isinstance(await record_bug("Lost state", "high", evidence), Rejection)
    assert not s.bugs and not s.screenshots
