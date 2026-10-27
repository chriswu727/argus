"""Runs under both MCP Python SDK 1.x and 2.x (see the mcp-2 CI job)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

from argus.entrypoints.mcp.compat import MCPServer, create_mcp_server, field
from argus.entrypoints.mcp.overhead import response_bytes
from argus.entrypoints.mcp.server import create_server, tool_names


def test_field_reads_wire_names_on_either_sdk():
    result = CallToolResult(
        content=[TextContent(type="text", text="ok")],
        structuredContent={"status": "completed"},
        isError=True,
    )
    assert field(result, "isError") is True
    assert field(result, "structuredContent") == {"status": "completed"}
    assert field(SimpleNamespace(server_info="snake"), "serverInfo") == "snake"
    assert field(SimpleNamespace(serverInfo="camel"), "serverInfo") == "camel"


def test_response_accounting_uses_the_compat_reader():
    result = CallToolResult(
        content=[TextContent(type="text", text="ok")], structuredContent={"a": 1}
    )
    counts = response_bytes(result)
    assert counts["structured_json_bytes"] == len('{"a":1}')
    assert counts["text_utf8_bytes"] == 2


def test_server_reports_product_version_and_delta_instructions(isolated_runtime):
    server = create_mcp_server("argus", "base", "9.9.9")
    assert isinstance(server, MCPServer)
    isolated_runtime.settings = replace(isolated_runtime.settings, response_mode="delta")
    delta = create_server(isolated_runtime, "web")
    assert "Delta mode" in delta.instructions


async def test_every_profile_lists_its_tools_with_output_schemas(isolated_runtime):
    for profile in ("web", "advanced", "screen", "full"):
        tools = await create_server(isolated_runtime, profile).list_tools()
        assert {tool.name for tool in tools} == tool_names(profile)
        assert all(field(tool, "outputSchema") for tool in tools)
