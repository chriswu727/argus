from __future__ import annotations

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from argus import __version__


async def test_real_stdio_handshake_and_local_page_journey(tmp_path):
    page = tmp_path / "fixture.html"
    page.write_text("<html><body><h1>Protocol fixture</h1><button>Save</button></body></html>")
    env = dict(os.environ, ARGUS_OUTPUT_DIR=str(tmp_path / "reports"))
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "argus.mcp_server", "--monitor-port", "0"], env=env
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "argus"
            assert initialized.serverInfo.version == __version__
            tools = await client.list_tools()
            assert len(tools.tools) == 29
            assert all(tool.outputSchema for tool in tools.tools)
            assert all("ctx" not in tool.inputSchema.get("properties", {}) for tool in tools.tools)
            finding_schema = next(
                tool for tool in tools.tools if tool.name == "record_bug"
            ).inputSchema
            evidence_schema = finding_schema["$defs"]["FindingEvidence"]["properties"]
            assert {"expected", "actual", "goal", "goals", "evidence_calls"} <= set(evidence_schema)
            missing = await client.call_tool("observe", {})
            assert missing.isError and "no_session" in missing.content[0].text
            assert missing.structuredContent["error_code"] == "no_session"
            progress = []

            async def on_progress(value, total, message):
                progress.append((value, total, message))

            started = await client.call_tool(
                "start_session",
                {"url": page.as_uri(), "goals": ["inspect page"]},
                progress_callback=on_progress,
            )
            assert not started.isError and "Protocol fixture" in started.content[0].text
            assert started.structuredContent["data"]["page"]["title"] == ""
            assert started.structuredContent["session_state"] == "active"
            assert started.structuredContent["stages"] == [
                "started",
                "launching",
                "navigating",
                "observing",
                "completed",
            ]
            assert len(progress) == 5
            assert [entry[0] for entry in progress] == list(range(1, 6))
            assert all(
                entry[1] is None and "Protocol fixture" not in entry[2] for entry in progress
            )
            rejected = await client.call_tool(
                "verify_persistence", {"expect": "maybe", "target_text": "fixture"}
            )
            assert (
                rejected.isError and rejected.structuredContent["error_code"] == "invalid_argument"
            )
            verified = await client.call_tool(
                "verify_persistence", {"expect": "present", "target_text": "Protocol fixture"}
            )
            assert verified.structuredContent["data"]["verification"]["matches"] is True
            assert verified.structuredContent["data"]["finding_confirmed"] is False
            shot = await client.call_tool("screenshot", {"name": "protocol"})
            assert [item.type for item in shot.content] == ["text", "image"]
            assert shot.structuredContent["status"] == "completed"
            finding = await client.call_tool(
                "record_bug",
                {
                    "title": "Synthetic contract finding",
                    "severity": "low",
                    "evidence": {
                        "description": None,
                        "steps": None,
                        "url": None,
                        "bug_type": None,
                        "expect_status": None,
                        "expected": "Expected contract state",
                        "actual": "Observed contract state",
                        "goal": "inspect page",
                        "evidence_calls": [2, 3],
                        "screenshot": "skip",
                    },
                },
            )
            assert not finding.isError
            assert finding.structuredContent["data"]["verdict"] == "inconclusive"
            rejected_end = await client.call_tool(
                "end_session",
                {
                    "coverage_updates": [
                        {
                            "goal": "inspect page",
                            "status": "exercised",
                            "evidence": "Inspected heading and Save control",
                        },
                        {"goal": "unknown", "status": "blocked", "evidence": "Unavailable"},
                    ]
                },
            )
            assert rejected_end.isError
            still_open = await client.call_tool("observe", {})
            assert still_open.structuredContent["session_state"] == "active"
            assert (
                still_open.structuredContent["data"]["coverage"]["goals"][0]["status"] == "untested"
            )
            ended = await client.call_tool(
                "end_session",
                {
                    "coverage_updates": [
                        {
                            "goal": "inspect page",
                            "status": "exercised",
                            "evidence": "Inspected heading and Save control",
                        }
                    ]
                },
            )
            assert not ended.isError and "Report saved" in ended.content[0].text
            assert ended.structuredContent["data"]["report_path"]
            assert ended.structuredContent["data"]["coverage"]["goals"][0]["status"] == "exercised"
    assert len(list((tmp_path / "reports").glob("report_*.json"))) == 1
    exported = json.loads(next((tmp_path / "reports").glob("report_*.json")).read_text())
    assert exported["findings"][0]["expected"] == "Expected contract state"
    assert exported["findings"][0]["actual"] == "Observed contract state"
    assert exported["findings"][0]["coverage_goals"] == ["inspect page"]
    assert exported["findings"][0]["claimed_evidence_calls"] == [2, 3]


async def test_stdio_enforces_startup_access_policy(tmp_path):
    fixture = tmp_path / "fixture.html"
    fixture.write_text("<html><body>Private fixture</body></html>")
    env = dict(
        os.environ,
        ARGUS_OUTPUT_DIR=str(tmp_path / "reports"),
        ARGUS_FILE_ROOTS="[]",
        ARGUS_ALLOWED_ORIGINS=json.dumps(["https://approved.test"]),
    )
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "argus.mcp_server", "--monitor-port", "0"], env=env
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            for url in [fixture.as_uri(), "https://unapproved.test", "data:text/plain,private"]:
                denied = await client.call_tool("start_session", {"url": url})
                assert denied.isError and "permission_denied" in denied.content[0].text
            empty = await client.call_tool("observe", {})
            assert empty.isError and "no_session" in empty.content[0].text


async def test_delta_stdio_resync_images_and_negative_persistence(tmp_path):
    page = tmp_path / "delta.html"
    page.write_text(
        '<html><body><label>Name<input id="name"></label><button onclick="document.getElementById(\'status\').textContent=\'Saved Morgan\'">Save</button><p id="status">Not saved</p>'
        + "".join(f"<button>Item {i}</button>" for i in range(40))
        + "</body></html>"
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "argus.mcp_server", "--tool-profile", "agent", "--response-mode", "delta"],
        env=dict(os.environ, ARGUS_OUTPUT_DIR=str(tmp_path / "reports")),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.version == __version__
            assert "base_id must match" in initialized.instructions
            start = await client.call_tool("start_session", {"url": page.as_uri()})
            base_id = start.structuredContent["data"]["snapshot"]["id"]
            action = await client.call_tool("click_what", {"description": "Save"})
            snapshot = action.structuredContent["data"]["snapshot"]
            assert snapshot["mode"] == "delta" and snapshot["base_id"] == base_id
            assert "Saved Morgan" in action.structuredContent["data"]["visible_text"]
            assert action.structuredContent["data"]["action"]["application_passed"] is None
            synced = await client.call_tool("observe", {})
            assert synced.structuredContent["data"]["snapshot"]["mode"] == "full"
            checked = await client.call_tool(
                "verify_persistence", {"expect": "present", "target_text": "Saved Morgan"}
            )
            assert checked.structuredContent["data"]["verification"]["matches"] is False
            assert checked.structuredContent["data"]["observation"]["snapshot"]["mode"] == "full"
            shot = await client.call_tool("screenshot", {"name": "delta"})
            assert [block.type for block in shot.content] == ["text", "image"]
            assert shot.structuredContent["data"]["artifacts"]
            ended = await client.call_tool("end_session", {})
            assert ended.structuredContent["data"]["report_path"]
