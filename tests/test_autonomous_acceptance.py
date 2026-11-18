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
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

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
    return [entry for file in files for entry in json.loads(file.read_text())["entries"]]


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
    assert report.is_file() and "Deleted task comes back after reload" in report.read_text()
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
    assert report.is_file() and "Deleted task comes back after reload" in report.read_text()
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
    report = Path(ended["data"]["report_path"]).read_text()
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
    assert test_file.is_file() and "def test_argus_finding" in test_file.read_text()
    ended, _ = await host.call("end_session")
    assert ended["data"]["summary"]["findings"] == 1
    assert test_file.is_file()

    late, text = await host.call("export_test", finding_id=finding_id)
    assert late["status"] == "error" and late["error_code"] == "no_session", text
