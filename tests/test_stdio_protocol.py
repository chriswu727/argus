from __future__ import annotations

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
            tools = await client.list_tools()
            assert len(tools.tools) == 30
            assert all(tool.outputSchema for tool in tools.tools)
            assert all("ctx" not in tool.inputSchema.get("properties", {}) for tool in tools.tools)
            missing = await client.call_tool("observe", {})
            assert missing.isError and "no_session" in missing.content[0].text
            assert missing.structuredContent["error_code"] == "no_session"
            progress = []

            async def on_progress(value, total, message):
                progress.append((value, total, message))

            started = await client.call_tool(
                "start_session", {"url": page.as_uri()}, progress_callback=on_progress
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
            ended = await client.call_tool("end_session", {})
            assert not ended.isError and "Report saved" in ended.content[0].text
            assert ended.structuredContent["data"]["report_path"]
    assert len(list((tmp_path / "reports").glob("report_*.json"))) == 1


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
