"""export_test: a verified finding becomes a test that fails now and passes once fixed."""

from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

import pytest

import argus.mcp_server as m
from argus.application.testgen import generate_test, locator_expr

from .conftest import make_element


class _TasksHandler(http.server.BaseHTTPRequestHandler):
    """A task list whose Delete hides the row but never deletes on the server,
    unless the app is 'fixed'."""

    tasks = ["Buy groceries", "Walk dog"]
    fixed = False
    confirm = False
    list_class = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        guard = "if (confirm('Delete it?')) " if type(self).confirm else ""
        rows = "".join(
            f"<li><span>{task}</span> <button onclick=\"{guard}fetch('/delete?name={task}', "
            f"{{method: 'POST'}}).then(() => this.parentElement.remove())\">Delete</button></li>"
            for task in type(self).tasks
        )
        ul = f'<ul class="{type(self).list_class}">' if type(self).list_class else "<ul>"
        body = f"<html><body><h1>Tasks</h1>{ul}{rows}</ul></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        if type(self).fixed and name in type(self).tasks:
            type(self).tasks.remove(name)
        self.send_response(200)
        self.end_headers()


def _serve():
    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = _Server(("127.0.0.1", 0), _TasksHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _fn(name):
    tool = getattr(m, name)
    return getattr(tool, "fn", tool)


@pytest.mark.parametrize(
    "confirm, list_class, task",
    [(False, "", "Buy groceries"), (True, "", "Buy groceries"), (False, "items", "Walk dog")],
    ids=["plain", "confirm-dialog", "row-inside-matching-list"],
)
async def test_exported_test_fails_while_the_bug_exists_and_passes_once_fixed(
    isolated_runtime, confirm, list_class, task
):
    # With a confirm dialog, the fixed app deletes only if the test accepts it.
    _TasksHandler.tasks = ["Buy groceries", "Walk dog"]
    _TasksHandler.fixed = False
    _TasksHandler.confirm = confirm
    _TasksHandler.list_class = list_class
    server, base = _serve()
    try:
        try:
            await _fn("start_session")(base + "/tasks")
        except Exception as exc:
            pytest.skip(f"Chromium/session unavailable: {exc}")
        # A list container that also matches the row selector must not make
        # the test click another row's Delete.
        await _fn("click_what")(f"Delete near {task}", accept_dialog=confirm)
        recorded = await _fn("record_bug")(
            "Deleted task comes back after reload",
            "high",
            {"expected": "The task stays deleted", "actual": "It is listed again"},
            verify={"expect": "present", "target_text": task, "at_url": base + "/tasks"},
        )
        finding_id = recorded.data["finding_id"]

        exported = await _fn("export_test")(finding_id, run=True)
        assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
        source = open(exported.data["path"]).read()
        assert f'_in_row(page, "{task}' in source
        assert "assert not _present(" in source

        _TasksHandler.fixed = True
        _TasksHandler.tasks = ["Buy groceries", "Walk dog"]
        after_fix = subprocess.run(
            [sys.executable, exported.data["path"]],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "ARGUS_BASE_URL": base},
        )
        assert after_fix.returncode == 0, after_fix.stderr[-500:]
    finally:
        await _fn("end_session")()
        server.shutdown()


async def test_only_verified_text_findings_are_exported(isolated_runtime):
    server, base = _serve()
    try:
        try:
            await _fn("start_session")(base + "/tasks")
        except Exception as exc:
            pytest.skip(f"Chromium/session unavailable: {exc}")
        recorded = await _fn("record_bug")(
            "Layout feels cramped", "low", {"expected": "Room", "actual": "Cramped"}
        )
        refused = await _fn("export_test")(recorded.data["finding_id"])
        assert "only findings verified" in refused
        assert "must identify" in await _fn("export_test")("nope:1")
    finally:
        await _fn("end_session")()
        server.shutdown()


def test_duplicate_controls_are_scoped_to_their_row():
    rows = []
    for title in ("Buy groceries", "Walk dog"):
        delete = make_element(len(rows), tag="button", text="Delete")
        delete.parent_context = f"{title} Delete"
        rows.append(delete)
    assert locator_expr(rows[1], rows) == (
        '_in_row(page, "Walk dog", '
        'lambda scope: scope.get_by_role("button", name="Delete", exact=True))'
    )
    lone = make_element(0, tag="button", text="Save")
    assert locator_expr(lone, [lone]) == 'page.get_by_role("button", name="Save", exact=True)'


def test_sensitive_inputs_and_unsupported_steps_are_not_guessed():
    secrets: dict = {}
    journey = [
        {
            "tool": "type_into",
            "description": "Password",
            "locator": 'page.locator("#pw")',
            "sensitive": True,
            "value_ref": "x",
        },
        {"tool": "drag_at", "description": "(1,2)->(3,4)"},
    ]
    source, notes = generate_test(
        finding_id="run:1",
        title="Login loses the session",
        expected="Stays logged in",
        receipt={
            "expect": "absent",
            "target_text": "Log out",
            "at_url": "http://h/app",
            "runs": "2/2",
        },
        journey=journey,
        start_url="http://h/login",
        resolve_input=lambda action: "secret-value",
        filename="t.py",
        secrets=secrets,
    )
    assert "secret-value" not in source
    assert secrets == {"ARGUS_INPUT_1": "secret-value"}
    assert 'os.environ["ARGUS_INPUT_1"]' in source
    assert "TODO(argus): replay step 2" in source and len(notes) == 1
    assert "page.once(" not in source
    assert 'assert _present("Log out", page)' in source
    compile(source, "t.py", "exec")


def test_an_accepted_dialog_is_accepted_only_during_its_step():
    journey = [
        {
            "tool": "click_what",
            "description": "Other",
            "locator": 'page.locator("#o")',
            "accept_dialog": True,
        },
        {"tool": "click_what", "description": "Delete", "locator": 'page.locator("#d")'},
    ]
    source, _ = generate_test(
        finding_id="run:1",
        title="Confirmed delete is kept",
        expected="Deleted",
        receipt={"expect": "present", "target_text": "Hello", "at_url": "http://h/"},
        journey=journey,
        start_url="http://h/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    body = source[source.index('page.goto(_url("/"))') :]
    other, delete = body.index('"#o"'), body.index('"#d"')
    on, off = (
        body.index('accept_dialog["pending"] = True'),
        body.index('accept_dialog["pending"] = False'),
    )
    assert on < other < off < delete
    compile(source, "t.py", "exec")


def test_a_restored_capsule_is_a_todo_at_its_position():
    from types import SimpleNamespace

    from argus.application.exports import _journey

    s = SimpleNamespace(
        action_trace=[
            {"tool": "navigate", "value": "/a"},
            {"tool": "click_what", "locator": 'page.locator("#go")'},
        ],
        capsule_restores=[(1, "logged-in"), (5, "later")],
    )
    journey = _journey(s, 2)
    assert [a["tool"] for a in journey] == ["navigate", "capsule_restore", "click_what"]
    source, notes = generate_test(
        finding_id="run:1",
        title="t",
        expected="",
        receipt={"expect": "present", "target_text": "x", "at_url": "http://h/"},
        journey=journey,
        start_url="http://h/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    assert len(notes) == 1 and "capsule 'logged-in'" in notes[0]
    assert "TODO(argus): step 2: establish the state of capsule 'logged-in'" in source


def test_backslashes_and_quotes_in_finding_text_keep_the_file_valid():
    import ast

    title = 'Path C:\\new\\x01 shows \\N and """quotes"""'
    source, _ = generate_test(
        finding_id="run:1",
        title=title,
        expected="Shows \\d+ items",
        receipt={"expect": "present", "target_text": "a\\b", "at_url": "http://h/"},
        journey=[],
        start_url="http://h/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    doc = ast.get_docstring(ast.parse(source))
    assert "C:\\new\\x01 shows \\N" in doc and "Shows \\d+ items" in doc


async def test_a_run_passes_sensitive_inputs_only_through_the_environment(tmp_path):
    from argus.application.testgen import run_exported_test

    script = tmp_path / "t.py"
    script.write_text(
        "import os\n"
        "assert os.environ.get('ARGUS_INPUT_1') == 'pw', 'ARGUS_SYMPTOM_PRESENT: no input'\n"
    )
    assert (await run_exported_test(script, 30))["outcome"] == "fails_on_symptom"
    secrets = {"ARGUS_INPUT_1": "pw"}
    assert (await run_exported_test(script, 30, secrets=secrets))["outcome"] == "passes"


@pytest.mark.skipif(os.name != "posix", reason="process groups")
@pytest.mark.parametrize("how", ["cancel", "timeout"])
async def test_a_stopped_run_kills_the_test_and_what_it_started(tmp_path, how):
    # The child stands in for the browser the exported test launches.
    import asyncio

    from argus.application.testgen import run_exported_test

    pids = tmp_path / "pids"
    script = tmp_path / "t.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    loop = asyncio.get_running_loop()
    if how == "cancel":
        task = asyncio.ensure_future(run_exported_test(script, 60))
        for _ in range(100):
            if pids.exists() and pids.read_text():
                break
            await asyncio.sleep(0.1)
        started = loop.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        started = loop.time()
        result = await run_exported_test(script, 2)
        assert result["outcome"] == "errored" and "timed out" in result["detail"]
    assert loop.time() - started < 15  # stopped, not waited out
    await asyncio.sleep(0.2)
    for pid in map(int, pids.read_text().split()):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        # A killed child not yet reaped by its (dead) parent is a zombie.
        state = open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0]
        assert state == "Z", (pid, state)


def test_hash_routes_are_kept_and_the_check_is_a_fresh_load():
    source, _ = generate_test(
        finding_id="run:1",
        title="t",
        expected="",
        receipt={"expect": "present", "target_text": "x", "at_url": "http://h/app?a=1#/tasks"},
        journey=[{"tool": "navigate", "value": "http://h/#/done"}],
        start_url="http://h/#/home",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    assert 'page.goto(_url("/#/home"))' in source
    assert 'page.goto(_url("/#/done"))' in source
    assert 'page.goto("about:blank")\n            page.goto(_url("/app?a=1#/tasks"))' in source


class _LazyHandler(http.server.BaseHTTPRequestHandler):
    """A long page whose last row renders only once it is scrolled near."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"""<html><body><h1>Rows</h1><div style="height:3000px"></div><ul id="rows"></ul>
<script>window.addEventListener('scroll', () => {
  if (window.scrollY > 1500 && !document.getElementById('late'))
    document.getElementById('rows').innerHTML = '<li id="late">Late row</li>';
});</script></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


async def test_the_exported_check_scrolls_like_the_verifier(tmp_path):
    from argus.application.testgen import run_exported_test

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = _Server(("127.0.0.1", 0), _LazyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        source, _ = generate_test(
            finding_id="run:1",
            title="Late row is missing",
            expected="Late row is listed",
            receipt={"expect": "absent", "target_text": "Late row", "at_url": base + "/"},
            journey=[],
            start_url=base + "/",
            resolve_input=lambda action: None,
            filename="t.py",
        )
        script = tmp_path / "t.py"
        script.write_text(source)
        # The row exists further down, so the "missing" symptom is not present.
        assert (await run_exported_test(script, 90))["outcome"] == "passes"
    finally:
        server.shutdown()


def test_a_row_is_keyed_by_its_leading_words_not_changing_details():
    def rows(*contexts):
        out = []
        for context in contexts:
            delete = make_element(len(out), tag="button", text="Delete")
            delete.parent_context = context
            out.append(delete)
        return out

    listed = rows(
        "Schedule team standup medium 5.0 days ago Edit Delete",
        "Buy groceries high 1.0 days ago Edit Delete",
    )
    assert '_in_row(page, "Schedule team",' in locator_expr(listed[0], listed)
    # A shorter key that another row contains is extended until it is unique.
    listed = rows("Walk dog Delete", "Walk dog later Delete")
    assert '_in_row(page, "Walk dog Delete",' in locator_expr(listed[0], listed)
    # Rows that cannot be told apart fall back to position.
    listed = rows("Same Delete", "Same Delete")
    assert locator_expr(listed[1], listed).endswith(".nth(1)")


def test_an_unnamed_checkbox_is_found_by_role_inside_its_row():
    toggle = make_element(0, tag="input", type="checkbox")
    toggle.css_path = "body > ul > li:nth-of-type(1) > input"
    toggle.parent_context = "Buy milk"
    assert locator_expr(toggle, [toggle]) == (
        '_in_row(page, "Buy milk", lambda scope: scope.get_by_role("checkbox"))'
    )
    # Another checkbox in the same row makes the role ambiguous: keep the path.
    starred = make_element(1, tag="input", type="checkbox", id="star-1")
    starred.parent_context = "Buy milk"
    assert locator_expr(toggle, [starred, toggle]) == (
        'page.locator("body > ul > li:nth-of-type(1) > input")'
    )
    toggle.parent_context = None
    assert locator_expr(toggle, [toggle]) == 'page.locator("body > ul > li:nth-of-type(1) > input")'


def test_helpers_are_written_only_when_the_test_uses_them():
    def source(journey):
        return generate_test(
            finding_id="run:1",
            title="t",
            expected="",
            receipt={"expect": "present", "target_text": "x", "at_url": "http://h/"},
            journey=journey,
            start_url="http://h/",
            resolve_input=lambda action: None,
            filename="t.py",
        )[0]

    plain = source([{"tool": "click_what", "locator": 'page.locator("#go")'}])
    assert "_in_row" not in plain and "ROW_SELECTOR" not in plain
    assert "accept_dialog" not in plain and "page.on(" not in plain
    scoped = source(
        [
            {
                "tool": "click_what",
                "locator": '_in_row(page, "A", lambda scope: scope.get_by_role("button"))',
                "accept_dialog": True,
            }
        ]
    )
    assert "def _in_row(" in scoped and "ROW_SELECTOR = " in scoped
    assert 'page.on("dialog", _on_dialog)' in scoped
    compile(plain, "t.py", "exec")
    compile(scoped, "t.py", "exec")
