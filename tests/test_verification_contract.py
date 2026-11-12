from __future__ import annotations

import http.server
import json
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from argus.application.actions import click_what
from argus.application.evidence import _journal_entries, _write_journal
from argus.application.exports import export_test
from argus.application.findings import record_bug
from argus.application.lifecycle import start_session
from argus.application.regression import RegressionRun, check_entries
from argus.application.verification import _run_reproduction_check
from argus.cli import _write_regression_artifact
from argus.domain.contracts import Rejection


@pytest.fixture
def contract_app():
    stopped = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        body = "<h1>Tasks</h1><p>GHOST-BUG</p>"
        rows = ["Buy groceries", "Walk dog"]
        posts = []
        redirect = False
        checks = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/poll":
                stopped.wait(60)
                return
            if self.redirect and self.path != "/login":
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            body = self.body
            status = 200
            if self.path == "/flaky":
                type(self).checks += 1
                body = "GHOST-BUG" if self.checks % 2 else "all clean"
                status = 404 if self.checks % 2 else 200
            elif self.path == "/flaky-text":
                type(self).checks += 1
                body = "GHOST-BUG" if self.checks % 2 else "all clean"
            if self.path == "/rows":
                body = (
                    "<h1>Tasks</h1><ul>"
                    + "".join(
                        f"<li><span>{name}</span> <button onclick=\"fetch('/delete?name={name}',"
                        f"{{method:'POST'}}).then(()=>this.parentElement.remove())\">Delete</button></li>"
                        for name in self.rows
                    )
                    + "</ul>"
                )
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            self.posts.append(parse_qs(urlparse(self.path).query)["name"][0])
            self.send_response(200)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Handler, f"http://127.0.0.1:{server.server_port}"
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join()


async def _record(target, **extra):
    return await record_bug(
        "Persistence symptom",
        "high",
        {"screenshot": "skip"},
        verify={"expect": "present", "target_text": target, **extra},
    )


@pytest.mark.parametrize("kind", ["text", "http_status"])
async def test_mixed_regression_is_inconclusive(isolated_runtime, monkeypatch, kind):
    async def verify(*args, **kwargs):
        return {"reproduced": False, "flaky": True, "runs": "1/2"}

    monkeypatch.setattr(isolated_runtime.verifiers, "verify", verify)
    results = await check_entries(
        None, [{"verify": {"kind": kind, "version": 1}}], "http://example.test"
    )
    assert results[0]["status"] == "INCONCLUSIVE"
    assert results[0]["runs"] == "1/2"
    assert RegressionRun(results).exit_code == 3


async def test_full_predicate_survives_record_journal_export(isolated_runtime, contract_app):
    handler, base = contract_app
    target = "A" * 121 + " tail"
    handler.body = f"<h1>Tasks</h1><p>{target}</p>"
    try:
        await start_session(base)
        recorded = await _record(target)
        bug = isolated_runtime.session.bugs[-1]
        assert bug.reproduction_receipt["reproduced"] is True
        assert len(bug.to_dict()["reproduction"]["target_text"]) <= 120
        _write_journal(isolated_runtime.session)
        entries = _journal_entries(urlparse(base).netloc)
        assert entries[0]["verify"]["target_text"] == target
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
        assert target not in Path(exported.data["path"]).read_text()
        handler.body = "<h1>Tasks</h1><p>all clean</p>"
        results = await check_entries(isolated_runtime.session, entries, base)
        assert results[0]["status"] == "FIXED"
        assert results[0]["runs"] == "0/2"
        assert RegressionRun(results).exit_code == 0
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "passes"
    finally:
        await isolated_runtime.shutdown()


async def test_storage_reset_cannot_be_silently_dropped(isolated_runtime, contract_app):
    handler, base = contract_app
    handler.body = """<h1>Profile</h1><button id="save" onclick="localStorage.setItem('name','SAVED-NAME');render()">Save</button>
<p id="out"></p><script>function render(){document.getElementById('out').textContent=localStorage.getItem('name')||''}render()</script>"""
    try:
        await start_session(base)
        await click_what("Save")
        recorded = await _record("SAVED-NAME", expect="absent", clear_storage=True)
        assert isolated_runtime.session.bugs[-1].reproduction_receipt["reproduced"] is True
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert "clear_storage" in str(exported)
        assert isinstance(exported, Rejection)
        assert not list(isolated_runtime.settings.output_dir.glob("tests/*.py"))
        _write_journal(isolated_runtime.session)
        entries = _journal_entries(urlparse(base).netloc)
        results = await check_entries(isolated_runtime.session, entries, base)
        assert results[0]["status"] == "INCONCLUSIVE"
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("redirect", [False, True])
async def test_expired_auth_form_is_not_absence_evidence(isolated_runtime, contract_app, redirect):
    handler, base = contract_app
    try:
        await start_session(base)
        handler.body = '<h1>Sign in</h1><input type="password"><button>Log in</button>'
        handler.redirect = redirect
        receipt = await _run_reproduction_check(
            isolated_runtime.session, {"expect": "absent", "target_text": "Saved name"}
        )
        assert receipt["reproduced"] is None, receipt
        assert "login wall" in receipt["reason"]
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("path", ["", "/login"])
async def test_intentional_login_page_check_exports(isolated_runtime, contract_app, path):
    handler, base = contract_app
    handler.body = (
        '<h1>Sign in</h1><input type="password"><button>Log in</button><p>Broken validation</p>'
    )
    try:
        await start_session(base + path)
        recorded = await _record("Broken validation")
        assert isolated_runtime.session.bugs[-1].reproduction_receipt["reproduced"] is True
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
        handler.body = '<h1>Sign in</h1><input type="password"><button>Log in</button>'
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "passes", exported
        _write_journal(isolated_runtime.session)
        entries = _journal_entries(urlparse(base).netloc)
        results = await check_entries(isolated_runtime.session, entries, base)
        assert results[0]["status"] == "FIXED", results
        assert RegressionRun(results).exit_code == 0
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("change", ["similar", "gone", "duplicate", "unchanged"])
async def test_row_identity_before_any_write(isolated_runtime, contract_app, change):
    handler, base = contract_app
    try:
        await start_session(base + "/rows")
        await click_what("Delete near Buy groceries")
        assert handler.posts == ["Buy groceries"]
        recorded = await _record("Buy groceries")
        handler.rows = {
            "similar": ["Buy groceries tomorrow", "Buy groceries", "Walk dog"],
            "gone": ["Buy groceries tomorrow", "Walk dog"],
            "duplicate": ["Buy groceries", "Buy groceries", "Walk dog"],
            "unchanged": ["Buy groceries", "Walk dog"],
        }[change]
        handler.posts.clear()
        exported = await export_test(recorded.data["finding_id"], run=True)
        if change in {"gone", "duplicate"}:
            assert handler.posts == [], exported
            assert exported.data["run"]["outcome"] == "errored"
        else:
            assert handler.posts == ["Buy groceries"], exported
            assert exported.data["run"]["outcome"] == "fails_on_symptom"
    finally:
        await isolated_runtime.shutdown()


async def test_polling_does_not_hide_observed_symptom(isolated_runtime, contract_app):
    handler, base = contract_app
    handler.body += '<script>fetch("/poll")</script>'
    try:
        await start_session(base)
        recorded = await _record("GHOST-BUG")
        assert isolated_runtime.session.bugs[-1].reproduction_receipt["reproduced"] is True
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
        handler.body = '<h1>Tasks</h1><script>fetch("/poll")</script>'
        exported = await export_test(recorded.data["finding_id"], run=True)
        assert exported.data["run"]["outcome"] == "inconclusive", exported
        assert exported.data["run"]["reason"] == "unsettled"
        _write_journal(isolated_runtime.session)
        entries = _journal_entries(urlparse(base).netloc)
        results = await check_entries(isolated_runtime.session, entries, base)
        assert results[0]["status"] == "INCONCLUSIVE"
    finally:
        await isolated_runtime.shutdown()


async def test_unsettled_absence_is_inconclusive(isolated_runtime, contract_app):
    handler, base = contract_app
    handler.body = '<h1>Loading</h1><script>fetch("/poll")</script>'
    try:
        await start_session(base)
        receipt = await _run_reproduction_check(
            isolated_runtime.session, {"expect": "absent", "target_text": "Saved name"}
        )
        assert receipt["reproduced"] is None, receipt
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("kind", ["text", "http_status"])
async def test_real_mixed_receipt_keeps_cli_artifacts_inconclusive(
    isolated_runtime, contract_app, tmp_path, kind
):
    _, base = contract_app
    clause = {"version": 1, "kind": kind}
    clause.update(
        {"expect_status": 404, "at_url": base + "/flaky"}
        if kind == "http_status"
        else {"expect": "present", "target_text": "GHOST-BUG", "at_url": base + "/flaky-text"}
    )
    try:
        await start_session(base)
        results = await check_entries(isolated_runtime.session, [{"verify": clause}], base)
        assert results[0]["runs"] == "1/2", results
        assert results[0]["status"] == "INCONCLUSIVE"
        assert RegressionRun(results).exit_code == 3
        artifact = _write_regression_artifact(str(tmp_path), base, results)
        doc = json.loads(Path(artifact + ".json").read_text())
        assert doc["summary"]["fixed"] == 0
        assert doc["summary"]["inconclusive"] == 1
        assert ET.parse(artifact + ".junit.xml").find("testcase/skipped") is not None
    finally:
        await isolated_runtime.shutdown()


@pytest.mark.parametrize("spoof", [False, True])
async def test_login_redirect_before_record_does_not_confirm_absence(
    isolated_runtime, contract_app, spoof
):
    handler, base = contract_app
    handler.redirect = True
    handler.body = '<h1>Sign in</h1><input type="password"><button>Log in</button>'
    try:
        await start_session(base + "/notes")
        recorded = await _record("My note", expect="absent", login_page=spoof, _login_page=spoof)
        assert recorded.data["receipt"]["reproduced"] is None, recorded
        assert "login wall" in recorded.data["receipt"]["reason"]
    finally:
        await isolated_runtime.shutdown()


async def test_regression_keeps_original_non_login_context(isolated_runtime, contract_app):
    handler, base = contract_app
    try:
        await start_session(base)
        await _record("GHOST-BUG")
        _write_journal(isolated_runtime.session)
        entries = _journal_entries(urlparse(base).netloc)
        handler.redirect = True
        handler.body = '<h1>Sign in</h1><input type="password"><button>Log in</button>'
        await isolated_runtime.session.browser.goto(base)
        results = await check_entries(isolated_runtime.session, entries, base)
        assert results[0]["status"] == "INCONCLUSIVE", results
        assert RegressionRun(results).exit_code == 3
    finally:
        await isolated_runtime.shutdown()


async def test_recheck_keeps_full_predicate_out_of_public_history(isolated_runtime, contract_app):
    handler, base = contract_app
    target = "A" * 121 + " private-tail"
    try:
        await start_session(base)
        recorded = await _record(target)
        assert isolated_runtime.session.bugs[-1].reproduction_receipt["reproduced"] is False
        handler.body = f"<p>{target}</p>"
        await record_bug(
            "Persistence symptom",
            "high",
            finding_id=recorded.data["finding_id"],
            verify={"expect": "present", "target_text": target},
        )
        bug = isolated_runtime.session.bugs[-1]
        assert bug.reproduction_receipt["reproduced"] is True
        assert bug.verification_spec["target_text"] == target
        assert target not in json.dumps(bug.to_dict())
    finally:
        await isolated_runtime.shutdown()


async def test_legacy_journal_requires_explicit_revalidation(isolated_runtime, monkeypatch):
    async def should_not_run(*args, **kwargs):
        raise AssertionError("legacy check must not execute with unknown preconditions")

    monkeypatch.setattr(isolated_runtime.verifiers, "verify", should_not_run)
    results = await check_entries(None, [{"verify": {"expect_status": 404}}], "http://example.test")
    assert results[0]["status"] == "INCONCLUSIVE"
    assert "legacy" in results[0]["reason"]
    assert "record" in results[0]["reason"]
    assert RegressionRun(results).exit_code == 3


def test_fingerprints_keep_full_targets_and_url_routes_distinct():
    from argus.application.evidence import _bug_fingerprint
    from argus.domain.models import Bug, BugType, Severity

    bug = Bug(BugType.UX_ISSUE, Severity.HIGH, "t", "d", "http://example.test", [])
    fingerprints = set()
    for target in ("A" * 121 + " one", "A" * 121 + " two"):
        for url in (
            "http://example.test/?id=1#first",
            "http://example.test/?id=2#first",
            "http://example.test/?id=2#second",
        ):
            bug.verification_spec = {
                "version": 1,
                "expect": "present",
                "target_text": target,
                "at_url": url,
            }
            fingerprint = _bug_fingerprint(bug)
            assert target not in fingerprint and url not in fingerprint
            fingerprints.add(fingerprint)
    assert len(fingerprints) == 6


async def test_truncated_row_context_requires_translation_before_replay(tmp_path):
    from argus.application.testgen import generate_test, locator_expr, run_exported_test
    from argus.domain.models import InteractiveElement

    target = InteractiveElement(0, "button", text="Delete", parent_context="A" * 200)
    other = InteractiveElement(1, "button", text="Delete", parent_context="Other Delete")
    locator = locator_expr(target, [target, other])
    assert locator is None
    path = tmp_path / "export.py"
    secrets = {}
    source, notes = generate_test(
        finding_id="run:1",
        title="t",
        expected="",
        receipt={"expect": "present", "target_text": "x"},
        journey=[{"tool": "click_what", "locator": locator}],
        start_url="http://127.0.0.1:1/",
        resolve_input=lambda action: None,
        filename=path.name,
        secrets=secrets,
    )
    assert any("complete, unambiguous locator" in note for note in notes)
    path.write_text(source)
    assert (await run_exported_test(path, 10, secrets=secrets))["outcome"] == "incomplete"
