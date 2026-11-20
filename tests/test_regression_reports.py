"""regression_check results reach the end-of-session summary and every report.

A real host re-tested a fix with regression_check and the report it handed the
user had no recheck in it: summary.total 0, no findings, nothing in the HTML,
JSON or JUnit. These tests read what a user would read.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SECRET = "SYNTHETIC_SECRET_77"


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


def _server(tmp_path):
    env = dict(os.environ, ARGUS_OUTPUT_DIR=str(tmp_path / "reports"))
    return StdioServerParameters(command=sys.executable, args=["-m", "argus.mcp_server"], env=env)


async def _record(call, url):
    data = (
        await call(
            "record_bug",
            {
                "title": f"Ghost remains token={SECRET}",
                "severity": "high",
                "evidence": {"screenshot": "skip"},
                "verify": {"expect": "present", "target_text": "GHOST-BUG", "at_url": url},
            },
        )
    ).structuredContent["data"]
    assert data["verdict"] == "verified"


def _reports(report_path):
    path = Path(report_path)
    return (
        path.read_text(encoding="utf-8"),
        json.loads(path.with_suffix(".json").read_text(encoding="utf-8")),
        ET.parse(path.with_suffix(".junit.xml")).getroot(),
        "".join(p.read_text(encoding="utf-8") for p in path.parent.glob(path.stem + ".*")),
    )


def _sarif_run(report_path):
    (run,) = json.loads(Path(report_path).with_suffix(".sarif").read_text(encoding="utf-8"))["runs"]
    return run


def _journal(tmp_path):
    folder = tmp_path / "reports" / ".argus" / "journal"
    return [
        e
        for p in folder.glob("*.json")
        for e in json.loads(p.read_text(encoding="utf-8"))["entries"]
    ]


async def test_repeated_rechecks_keep_the_latest_status_and_the_history(site, tmp_path):
    handler, url = site
    async with stdio_client(_server(tmp_path)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                return result

            await call("start_session", {"url": url})
            await _record(call, url)
            await call("end_session")

            await call("start_session", {"url": url, "review_mode": "regression"})
            for state in ("buggy", "unavailable"):
                handler.state = state
                await call("regression_check")
            ended = await call("end_session")

    data = ended.structuredContent["data"]
    regression = data["regression"]
    assert regression["checks"] == 2
    assert regression["counts"] == {
        "still_present": 0,
        "no_longer_reproduces": 0,
        "inconclusive": 1,
    }
    (finding,) = regression["findings"]
    # The last check could not tell; the earlier one saw the bug. Both stay visible.
    assert finding["status"] == "INCONCLUSIVE"
    assert finding["history"] == ["STILL-PRESENT", "INCONCLUSIVE"]
    assert "503" in finding["reason"] and finding["path"] == "/"
    assert data["summary"]["findings"] == 0

    text = ended.content[0].text
    assert "Regression re-check (latest of 2 run(s)): 0 still present" in text
    assert "[INCONCLUSIVE] [HIGH] Ghost remains" in text and SECRET not in text

    html, report, junit, everything = _reports(data["report_path"])
    assert report["findings"] == [] and report["summary"]["total"] == 0
    assert report["regression"]["findings"][0]["history"] == ["STILL-PRESENT", "INCONCLUSIVE"]
    assert "Regression Re-check (1)" in html and "STILL-PRESENT → INCONCLUSIVE" in html
    # On a narrow screen the table scrolls in its own box, not the whole page.
    assert (
        '<div class="rw"><table class="rt">' in html
        and ".rw{max-width:100%;overflow-x:auto" in html
    )
    (case,) = junit.findall("testcase")
    assert case.get("classname") == "argus.regression.high"
    assert case.find("skipped") is not None and case.find("failure") is None
    assert junit.get("failures") == "0" and junit.get("tests") == "1"
    (result,) = _sarif_run(data["report_path"])["results"]
    assert result["kind"] == "open" and result["level"] == "none"
    assert result["properties"]["history"] == ["STILL-PRESENT", "INCONCLUSIVE"]
    assert SECRET not in everything and SECRET not in json.dumps(ended.structuredContent)
    assert len(_journal(tmp_path)) == 1


@pytest.mark.parametrize(
    "state,status,failures",
    [("buggy", "STILL-PRESENT", "1"), ("fixed", "NO-LONGER-REPRODUCES", "0")],
)
async def test_each_outcome_reaches_the_junit_gate(site, tmp_path, state, status, failures):
    handler, url = site
    async with stdio_client(_server(tmp_path)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                return result

            await call("start_session", {"url": url})
            await _record(call, url)
            await call("end_session")
            handler.state = state
            await call("start_session", {"url": url, "review_mode": "regression"})
            await call("regression_check")
            ended = await call("end_session")

    data = ended.structuredContent["data"]
    assert data["regression"]["findings"][0]["status"] == status
    _, report, junit, _ = _reports(data["report_path"])
    assert report["findings"] == [] and report["regression"]["findings"][0]["status"] == status
    assert junit.get("failures") == failures
    (case,) = junit.findall("testcase")
    assert (case.find("failure") is not None) is (status == "STILL-PRESENT")
    assert case.find("skipped") is None
    (result,) = _sarif_run(data["report_path"])["results"]
    if status == "STILL-PRESENT":
        assert result["kind"] == "fail" and result["level"] == "error"
    else:
        assert result["kind"] == "pass" and result["level"] == "none"
    html, _, _, _ = _reports(data["report_path"])
    # A known bug that is still there must not sit under a plain "No bugs found".
    assert "No bugs found" not in html and "Bugs Found" not in html
    assert "No new findings recorded in this session." in html
    assert len(_journal(tmp_path)) == 1


async def test_a_session_left_open_still_reports_its_rechecks(site, tmp_path):
    """Starting again without end_session saves the earlier session's report,
    rechecks included, as it does for findings."""
    _, url = site
    async with stdio_client(_server(tmp_path)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                return result

            await call("start_session", {"url": url})
            await _record(call, url)
            await call("end_session")
            await call("start_session", {"url": url, "review_mode": "regression"})
            await call("regression_check")
            restarted = await call("start_session", {"url": url})
            await call("end_session")

    unended = restarted.structuredContent["data"]["unended_run"]
    _, report, _, _ = _reports(unended["report_path"])
    assert report["regression"]["findings"][0]["status"] == "STILL-PRESENT"
    assert report["findings"] == []


async def test_a_recheck_with_nothing_journaled_is_still_reported(site, tmp_path):
    """Calling regression_check where nothing was journaled is a result too:
    nothing was rechecked, and the report says so instead of saying nothing."""
    _, url = site
    async with stdio_client(_server(tmp_path)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, arguments=None):
                result = await client.call_tool(name, arguments or {})
                assert not result.isError, result.content
                return result

            await call("start_session", {"url": url, "review_mode": "regression"})
            await call("regression_check")
            ended = await call("end_session")

    data = ended.structuredContent["data"]
    assert data["regression"]["checks"] == 1 and data["regression"]["findings"] == []
    assert data["regression"]["reason"] == "no_journal"
    assert "no journaled findings for this site; nothing was rechecked" in ended.content[0].text
    html, report, junit, _ = _reports(data["report_path"])
    assert report["regression"]["reason"] == "no_journal" and report["findings"] == []
    assert "Regression Re-check (0)" in html and "nothing was rechecked" in html
    assert "No bugs found" not in html
    props = {p.get("name"): p.get("value") for p in junit.iter("property")}
    assert props == {"regression_checks": "1", "regression_reason": "no_journal"}
    assert junit.findall("testcase") == []
    run = _sarif_run(data["report_path"])
    assert run["results"] == [] and run["properties"]["regression"]["reason"] == "no_journal"


async def test_a_session_without_rechecks_reports_as_before(site, tmp_path):
    _, url = site
    async with stdio_client(_server(tmp_path)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool("start_session", {"url": url})
            ended = await client.call_tool("end_session", {})

    data = ended.structuredContent["data"]
    assert data["regression"] is None and "Regression re-check" not in ended.content[0].text
    html, report, junit, _ = _reports(data["report_path"])
    assert "regression" not in report and "No bugs found." in html
    assert junit.find("properties") is None
    assert "properties" not in _sarif_run(data["report_path"])
