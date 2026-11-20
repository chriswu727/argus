"""What a host agent meets when it tests an app without a person helping.

Each test drives the MCP tools the way a host does (through the server, so
timeouts, session state and error results are the real ones) against a small
application whose defects can be switched on and off. A defective build, the
fixed build and a build that never had the defect are all run, so a check
that always reports a bug, or never does, fails here.

These are scripted calls. They show that the tools give a host what it needs
to finish on its own; they do not show that a model chooses to make the calls.
"""

from __future__ import annotations

import asyncio
import html
import http.server
import json
import os
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import argus.mcp_server as m
from argus.domain.contracts import SessionState

TASK = "Buy groceries"


class _App:
    def __init__(self):
        self.tasks = [TASK, "Walk dog"]
        self.notes: list[str] = []
        self.flash = ""
        self.hidden_once = ""
        self.delete_persists = True
        self.save_hangs = False
        self.signed_in = False
        self.note_writes = 0


def _handler(app: _App):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _page(self, body, status=200):
            data = f"<!doctype html><title>Tasks</title>{body}".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/tasks":
                flash, app.flash = app.flash, ""
                # The defect: the page right after a delete leaves the task out,
                # but nothing was removed, so the next load lists it again.
                hidden, app.hidden_once = app.hidden_once, ""
                rows = "".join(
                    f"<li><span>{html.escape(task)}</span><form method='post' action='/delete'>"
                    f"<input type='hidden' name='task' value='{html.escape(task)}'>"
                    "<button>Delete</button></form></li>"
                    for task in app.tasks
                    if task != hidden
                )
                toast = f"<div role='status'>{flash}</div>" if flash else ""
                shown = len(app.tasks) - (hidden in app.tasks)
                self._page(
                    f"<h1>Tasks</h1>{toast}<ul>{rows}</ul><p>{shown} tasks</p>"
                    "<a href='/notes'>Notes</a> <a href='/account'>Account</a>"
                )
            elif path == "/notes":
                saved = "".join(f"<li>{html.escape(note)}</li>" for note in app.notes)
                self._page(
                    "<h1>Notes</h1><form method='post' action='/notes'>"
                    "<label>Note <input name='note'></label><button>Save</button></form>"
                    f"<ul>{saved}</ul>"
                )
            elif path == "/slow":
                self._page(
                    "<h1>Reports</h1><p id='state'>Loading…</p><script>setTimeout(()=>{"
                    "document.getElementById('state').outerHTML="
                    "'<button>Open report</button>'},1500)</script>"
                )
            elif path == "/account":
                if app.signed_in:
                    self._page("<h1>Account</h1><p>Plan: Free</p>")
                else:
                    self._redirect("/login")
            elif path == "/login":
                self._page(
                    "<h1>Sign in</h1><form><label>Email <input name='email'></label>"
                    "<label>Password <input type='password' name='password'></label>"
                    "<button>Sign in</button></form>"
                )
            else:
                self._page("<h1>Not found</h1>", 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode())
            path = urlparse(self.path).path
            if path == "/delete":
                task = form.get("task", [""])[0]
                if app.delete_persists and task in app.tasks:
                    app.tasks.remove(task)
                else:
                    app.hidden_once = task
                app.flash = "Task deleted"
                self._redirect("/tasks")
            elif path == "/notes":
                app.notes.append(form.get("note", [""])[0])
                app.note_writes += 1
                if app.save_hangs:
                    time.sleep(8)
                self._redirect("/notes")

    return Handler


@pytest.fixture
def app():
    state = _App()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


class _Host:
    """Counts what a host would count: calls made and calls that failed."""

    def __init__(self):
        self.calls = 0
        self.errors = 0

    async def call(self, name, **arguments):
        self.calls += 1
        result = await m.mcp.call_tool(name, arguments)
        envelope = result.structuredContent
        text = "\n".join(item.text for item in result.content if getattr(item, "text", None))
        if result.isError:
            self.errors += 1
        return envelope, text


async def _start(host, url, **arguments):
    envelope, text = await host.call("start_session", url=url, **arguments)
    if envelope["status"] != "completed":
        pytest.skip(f"Chromium/session unavailable: {text}")
    return envelope, text


def _journal(runtime) -> list:
    files = list((Path(runtime.settings.output_dir) / ".argus" / "journal").glob("*.json"))
    return [
        entry for file in files for entry in json.loads(file.read_text(encoding="utf-8"))["entries"]
    ]


async def _delete_and_check(host, app):
    """The journey for "deleting a task": delete, then look again after a reload."""
    acted, _ = await host.call(
        "test_action", target="Delete in the Buy groceries row", expect={"removes": TASK}
    )
    assert acted["status"] == "completed", acted
    checked, _ = await host.call(
        "verify_persistence", expect="absent", target_text=TASK, after_url="/tasks"
    )
    return checked["data"]


async def test_a_delete_that_does_not_stay_is_found_reported_and_rechecked(app, isolated_runtime):
    host = _Host()
    app.delete_persists = False
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    check = await _delete_and_check(host, app)
    assert check["verification"]["matches"] is False
    assert check["suggested_verify"]["expect"] == "present"
    recorded, text = await host.call(
        "record_bug",
        title="Deleted task comes back after reload",
        severity="high",
        evidence={"expected": "the task stays deleted", "actual": "it is listed again"},
        verify=check["suggested_verify"],
    )
    assert "CONFIRMED 2/2" in text
    # Journaled when confirmed, so a session that is never ended can still be rechecked.
    assert [entry["title"] for entry in _journal(isolated_runtime)] == [
        "Deleted task comes back after reload"
    ]
    ended, text = await host.call(
        "end_session",
        coverage_updates=[
            {"goal": "Delete a task", "status": "exercised", "evidence": "task returned"}
        ],
    )
    report = Path(ended["data"]["report_path"])
    assert report.is_file() and "Deleted task comes back after reload" in report.read_text(
        encoding="utf-8"
    )
    assert ended["data"]["summary"]["findings"] == 1
    assert host.errors == 0
    discovery_calls = host.calls

    # Not fixed yet: the same check says so.
    _, text = await _start(host, app.base + "/tasks", review_mode="regression")
    assert "1 finding(s) from prior runs are journaled" in text
    _, text = await host.call("regression_check")
    assert "STILL-PRESENT" in text and "1 still present" in text
    await host.call("end_session")

    # Fixed: repeat the action once on the fixed build, then recheck from the journal.
    app.delete_persists = True
    await _start(host, app.base + "/tasks", review_mode="regression")
    assert (await _delete_and_check(host, app))["verification"]["matches"] is True
    _, text = await host.call("regression_check")
    assert "NO-LONGER-REPRODUCES" in text and "0 still present" in text
    await host.call("end_session")
    assert host.errors == 0
    assert discovery_calls <= 6 and host.calls - discovery_calls <= 10


async def test_a_build_without_the_defect_ends_with_no_findings(app, isolated_runtime):
    host = _Host()
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    check = await _delete_and_check(host, app)
    assert check["verification"]["matches"] is True
    assert check["suggested_verify"] is None
    ended, text = await host.call(
        "end_session",
        coverage_updates=[
            {"goal": "Delete a task", "status": "exercised", "evidence": "gone after reload"}
        ],
    )
    assert ended["data"]["summary"]["findings"] == 0 and "Bugs found: 0" in text
    assert Path(ended["data"]["report_path"]).is_file()
    assert _journal(isolated_runtime) == []
    await _start(host, app.base + "/tasks", review_mode="regression")
    _, text = await host.call("regression_check")
    assert "no journaled findings" in text
    await host.call("end_session")
    assert host.errors == 0


async def test_content_that_arrives_late_is_seen_by_observing_again(app):
    host = _Host()
    envelope, text = await _start(host, app.base + "/slow")
    assert "Open report" not in text
    for _ in range(3):
        # A host takes at least this long between two calls.
        await asyncio.sleep(1)
        envelope, text = await host.call("observe")
        if "Open report" in text:
            break
    assert "Open report" in text
    clicked, _ = await host.call("click_what", description="Open report")
    assert clicked["status"] == "completed"
    await host.call("end_session")


async def test_a_row_that_is_gone_is_refused_not_replaced_by_its_neighbour(app):
    host = _Host()
    await _start(host, app.base + "/tasks")
    await _delete_and_check(host, app)
    assert app.tasks == ["Walk dog"]
    stale, text = await host.call("click_what", description="Delete in the Buy groceries row")
    assert stale["status"] == "error", text
    assert app.tasks == ["Walk dog"]
    _, text = await host.call("observe")
    assert "Walk dog" in text and TASK not in text
    await host.call("end_session")


async def test_after_a_timeout_the_write_is_checked_and_nothing_found_is_lost(
    app, isolated_runtime
):
    host = _Host()
    await _start(
        host, app.base + "/tasks", goals=["Delete a task", "Save a note", "Change the plan"]
    )
    app.delete_persists = False
    check = await _delete_and_check(host, app)
    await host.call(
        "record_bug",
        title="Deleted task comes back after reload",
        severity="high",
        evidence={"expected": "the task stays deleted", "actual": "it is listed again"},
        verify=check["suggested_verify"],
    )
    await host.call("coverage_update", goal="Delete a task", status="exercised", evidence="found")
    for step in (
        await host.call("navigate", url="/notes"),
        await host.call("type_into", description="Note", text="Call the plumber"),
    ):
        assert step[0]["status"] == "completed", step[1]

    app.save_hangs = True
    isolated_runtime.settings = replace(isolated_runtime.settings, operation_timeout=4)
    saved, text = await host.call("click_what", description="Save")
    isolated_runtime.settings = replace(isolated_runtime.settings, operation_timeout=120)
    app.save_hangs = False
    assert saved["error_code"] == "timeout", text
    assert "check whether the change happened before repeating it" in text
    assert "start_session to continue" in text
    assert isolated_runtime.state == SessionState.FAILED

    refused, text = await host.call("observe")
    assert refused["status"] == "error" and "findings are kept" in text

    started, text = await _start(
        host, app.base + "/notes", goals=["Save a note", "Change the plan"]
    )
    previous = started["data"]["unended_run"]
    assert previous["findings"] == 1
    assert previous["unfinished_goals"] == ["Save a note", "Change the plan"]
    report = Path(previous["report_path"])
    assert report.is_file() and "Deleted task comes back after reload" in report.read_text(
        encoding="utf-8"
    )
    assert "was not ended" in text and str(report) in text

    # The save did happen. Seeing that, the host does not send it again.
    check, _ = await host.call(
        "verify_persistence", expect="present", target_text="Call the plumber", after_url="/notes"
    )
    assert check["data"]["verification"]["matches"] is True
    assert app.note_writes == 1
    await host.call("end_session")
    assert len(_journal(isolated_runtime)) == 1


async def test_a_page_behind_a_login_is_reported_as_blocked_not_as_a_bug(app, isolated_runtime):
    host = _Host()
    await _start(host, app.base + "/tasks", goals=["Delete a task", "Change the plan"])
    landed, _ = await host.call("navigate", url="/account")
    assert landed["data"]["page"]["url"].endswith("/login")
    assert "Sign in" in landed["data"]["visible_text"]
    check, text = await host.call(
        "verify_persistence", expect="absent", target_text="Plan: Free", after_url="/account"
    )
    assert check["data"]["verification"]["matches"] is None and "INCONCLUSIVE" in text
    assert check["data"]["suggested_verify"] is None
    ended, text = await host.call(
        "end_session",
        coverage_updates=[
            {
                "goal": "Change the plan",
                "status": "blocked",
                "evidence": "/account redirects to the sign-in page; no account was provided",
            }
        ],
    )
    summary = ended["data"]["coverage"]["summary"]
    assert summary["blocked"] == 1 and ended["data"]["summary"]["findings"] == 0
    assert "Unfinished goals: Delete a task" in text
    report = Path(ended["data"]["report_path"]).read_text(encoding="utf-8")
    assert "no account was provided" in report
    assert _journal(isolated_runtime) == []


async def test_a_restart_that_fails_still_says_where_the_findings_went(app, isolated_runtime):
    host = _Host()
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    app.delete_persists = False
    check = await _delete_and_check(host, app)
    await host.call(
        "record_bug",
        title="Deleted task comes back after reload",
        severity="high",
        evidence={"expected": "the task stays deleted", "actual": "it is listed again"},
        verify=check["suggested_verify"],
    )
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        closed_port = unused.getsockname()[1]
    failed, text = await host.call("start_session", url=f"http://127.0.0.1:{closed_port}/")
    assert failed["status"] == "error" and "No session was created" in text
    reports = list(Path(isolated_runtime.settings.output_dir).glob("*.html"))
    assert len(reports) == 1 and str(reports[0]) in text
    assert "Goals it left unfinished (pass them as goals to continue): Delete a task" in text
    ended, text = await host.call("end_session")
    assert ended["status"] == "completed"


async def test_a_requested_test_file_is_exported_before_the_session_is_closed(app):
    """A user who wants a regression test file gets it while the finding's
    session is open; a closed session has no findings left to export."""
    host = _Host()
    app.delete_persists = False
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    check = await _delete_and_check(host, app)
    recorded, _ = await host.call(
        "record_bug",
        title="Deleted task comes back after reload",
        severity="high",
        evidence={"expected": "the task stays deleted", "actual": "it is listed again"},
        verify=check["suggested_verify"],
    )
    finding_id = recorded["data"]["finding_id"]
    exported, text = await host.call("export_test", finding_id=finding_id)
    assert exported["status"] == "completed", text
    test_file = Path(exported["data"]["path"])
    assert test_file.is_file() and "def test_argus_finding" in test_file.read_text(encoding="utf-8")
    ended, _ = await host.call("end_session")
    assert ended["data"]["summary"]["findings"] == 1
    assert test_file.is_file()

    late, text = await host.call("export_test", finding_id=finding_id)
    assert late["status"] == "error" and late["error_code"] == "no_session", text


BUG = {
    "title": "Deleted task comes back after reload",
    "severity": "high",
    "evidence": {"expected": "Buy groceries stays deleted", "actual": "it is listed again"},
}


async def _failed_delete(host, app):
    app.delete_persists = False
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    check = await _delete_and_check(host, app)
    assert check["verification"]["matches"] is False
    return check


SYMPTOM = {"expect": "present", "target_text": TASK}


async def _confirm(host, recorded, title, severity="high"):
    """The host's own second call: it says this finding is that failure."""
    data = recorded["data"]
    return await host.call(
        "record_bug",
        title=title,
        severity=severity,
        finding_id=data["finding_id"],
        verify=data["suggested_verify"],
    )


async def test_a_finding_recorded_without_verify_is_confirmed_only_when_the_host_says_so(
    app, isolated_runtime
):
    """A real host saw the delete come back, recorded it and left verify out.
    Argus does not confirm it from the wording; it hands the check back."""
    host = _Host()
    await _failed_delete(host, app)
    recorded, text = await host.call("record_bug", **BUG)
    data = recorded["data"]
    assert data["verdict"] == "inconclusive" and data["receipt"] is None
    assert data["suggested_verify"] == {**SYMPTOM, "at_url": app.base + "/tasks"}
    assert "has not applied" in data["next_action"] and "not applied" in text
    assert _journal(isolated_runtime) == []

    confirmed, _ = await _confirm(host, recorded, BUG["title"])
    assert confirmed["data"]["verdict"] == "verified"
    assert confirmed["data"]["receipt"]["runs"] == "2/2"
    assert [entry["title"] for entry in _journal(isolated_runtime)] == [BUG["title"]]
    await host.call("end_session")


async def test_verify_stating_the_expected_state_keeps_its_verdict(app, isolated_runtime):
    """Another real host passed the state that should hold (absent) as the
    symptom. The claim as stated did not reproduce, and that is the verdict."""
    host = _Host()
    await _failed_delete(host, app)
    recorded, _ = await host.call(
        "record_bug",
        **BUG,
        verify={"expect": "absent", "target_text": TASK, "at_url": app.base + "/tasks"},
    )
    data = recorded["data"]
    assert data["verdict"] == "not_reproduced"
    assert data["receipt"]["expect"] == "absent" and data["receipt"]["runs"] == "0/2"
    assert "stated_expect" not in data["receipt"]
    assert data["suggested_verify"] == {**SYMPTOM, "at_url": app.base + "/tasks"}
    assert _journal(isolated_runtime) == []

    confirmed, _ = await _confirm(host, recorded, BUG["title"])
    assert confirmed["data"]["verdict"] == "verified"
    assert confirmed["data"]["receipt"]["expect"] == "present"
    assert len(_journal(isolated_runtime)) == 1
    await host.call("end_session")


@pytest.mark.parametrize("after_a_failed_check", [False, True])
async def test_a_claim_that_the_row_vanished_is_not_confirmed_while_it_is_there(
    app, isolated_runtime, after_a_failed_check
):
    """The row is on the page. A finding that says it disappeared, with verify
    saying so, does not reproduce, whatever an earlier check saw."""
    host = _Host()
    if after_a_failed_check:
        await _failed_delete(host, app)
    else:
        await _start(host, app.base + "/tasks")
    recorded, _ = await host.call(
        "record_bug",
        title="Buy groceries unexpectedly disappears from the saved list",
        severity="high",
        evidence={"expected": "Buy groceries is listed", "actual": "Buy groceries is missing"},
        verify={"expect": "absent", "target_text": TASK, "at_url": app.base + "/tasks"},
    )
    data = recorded["data"]
    assert data["verdict"] == "not_reproduced" and data["receipt"]["expect"] == "absent"
    assert data["receipt"]["observed_present"] == [True, True]
    await host.call("end_session")
    assert _journal(isolated_runtime) == []


@pytest.mark.parametrize(
    "title,evidence,suggestion_offered",
    [
        # Another page: the check on /tasks says nothing about /notes.
        (
            "Buy groceries appears in Notes without being saved there",
            {"url": "/notes", "expected": "Notes is empty", "actual": "Buy groceries is a note"},
            False,
        ),
        # The same page and the same row, but another claim. The button is there.
        (
            "Buy groceries is missing its Delete button",
            {"url": "/tasks", "actual": "Buy groceries has no Delete button"},
            True,
        ),
        # No page given, row named, nothing about persistence.
        ("The Buy groceries row is hard to read", {"actual": "Low contrast"}, True),
    ],
)
async def test_a_failed_check_never_confirms_another_finding(
    app, isolated_runtime, title, evidence, suggestion_offered
):
    """Naming the row is not a claim that the row came back. Nothing in a
    finding's words makes Argus attach the earlier check to it."""
    host = _Host()
    await _failed_delete(host, app)
    if "url" in evidence:
        evidence = {**evidence, "url": app.base + evidence["url"]}
    recorded, _ = await host.call("record_bug", title=title, severity="medium", evidence=evidence)
    data = recorded["data"]
    assert data["verdict"] == "inconclusive" and data["receipt"] is None
    assert data["evidence_status"] == "observed"
    assert (data["suggested_verify"] is not None) is suggestion_offered
    await host.call("end_session")
    assert _journal(isolated_runtime) == []


async def test_the_suggestion_ends_with_the_next_action(app, isolated_runtime):
    host = _Host()
    await _failed_delete(host, app)
    await host.call("navigate", url="/notes")
    late, _ = await host.call("record_bug", **BUG)
    assert late["data"]["receipt"] is None and late["data"]["suggested_verify"] is None
    assert "expect is what the page" in late["data"]["next_action"]
    await host.call("end_session")
    assert _journal(isolated_runtime) == []


@pytest.mark.parametrize("case", ["stated_expectation", "another_page", "another_claim"])
async def test_no_finding_is_verified_by_inference_over_stdio(app, tmp_path, case):
    """The same refusals through a real MCP stdio session, as a host sees them."""
    app.delete_persists = False
    env = dict(os.environ, ARGUS_OUTPUT_DIR=str(tmp_path / "reports"))
    server = StdioServerParameters(command=sys.executable, args=["-m", "argus.mcp_server"], env=env)
    arguments = {
        "stated_expectation": {
            "title": "Buy groceries is missing from saved tasks",
            "severity": "high",
            "evidence": {"expected": "Buy groceries is listed", "actual": "it is absent"},
            "verify": {"expect": "absent", "target_text": TASK, "at_url": app.base + "/tasks"},
        },
        "another_page": {
            "title": "Buy groceries leaked into Notes",
            "severity": "medium",
            "evidence": {"url": app.base + "/notes", "actual": "Buy groceries is in Notes"},
        },
        "another_claim": {
            "title": "Buy groceries is missing its Delete button",
            "severity": "medium",
            "evidence": {"url": app.base + "/tasks", "actual": "No Delete button"},
        },
    }[case]
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, payload):
                result = await client.call_tool(name, payload)
                assert not result.isError, result.content
                return result.structuredContent["data"]

            await call("start_session", {"url": app.base + "/tasks"})
            await call(
                "test_action",
                {"target": "Delete in the Buy groceries row", "expect": {"removes": TASK}},
            )
            check = await call(
                "verify_persistence",
                {"expect": "absent", "target_text": TASK, "after_url": "/tasks"},
            )
            assert check["verification"]["matches"] is False
            recorded = await call("record_bug", arguments)
            ended = await call("end_session", {})
    assert recorded["verdict"] in {"not_reproduced", "inconclusive"}, recorded
    report = json.loads(Path(ended["report_path"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert [finding["title"] for finding in report["findings"]] == [arguments["title"]]
    journal = Path(tmp_path / "reports" / ".argus" / "journal")
    assert not list(journal.glob("*.json"))


async def test_a_delete_that_stayed_leaves_nothing_to_confirm(app, isolated_runtime):
    host = _Host()
    await _start(host, app.base + "/tasks", goals=["Delete a task"])
    assert (await _delete_and_check(host, app))["verification"]["matches"] is True
    recorded, _ = await host.call("record_bug", **BUG)
    assert recorded["data"]["receipt"] is None
    wrong, _ = await host.call(
        "record_bug",
        title="Buy groceries is still listed",
        severity="high",
        verify={"expect": "present", "target_text": TASK, "at_url": app.base + "/tasks"},
    )
    assert wrong["data"]["verdict"] == "not_reproduced"
    assert _journal(isolated_runtime) == []
    await host.call("end_session")


async def test_a_goal_named_in_a_session_without_goals_does_not_cost_the_finding(app):
    """Seen in a real run: the session had been restarted without goals, the
    host still named one, and the whole finding was refused."""
    host = _Host()
    app.delete_persists = False
    await _start(host, app.base + "/tasks")
    check = await _delete_and_check(host, app)
    recorded, _ = await host.call(
        "record_bug",
        title=BUG["title"],
        severity="high",
        evidence={**BUG["evidence"], "goal": "Remove items from the basket"},
        verify=check["suggested_verify"],
    )
    data = recorded["data"]
    assert recorded["status"] == "completed" and data["verdict"] == "verified"
    assert any("goal not linked" in warning for warning in data["warnings"])
    ended, _ = await host.call("end_session")
    assert ended["data"]["summary"]["findings"] == 1
