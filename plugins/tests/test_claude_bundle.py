from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "plugins" / "claude"


def test_plugin_pins_the_published_runtime_and_safe_profile():
    manifest = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    config = json.loads((BUNDLE / ".mcp.json").read_text())
    assert manifest["name"] == "argus-testing"
    assert manifest["license"] == "MIT"
    assert config == {
        "mcpServers": {
            "argus": {
                "command": "uvx",
                "args": [
                    "--python",
                    "3.12",
                    "--from",
                    f"argus-testing=={manifest['version']}",
                    "argus-mcp",
                    "--tool-profile",
                    "web",
                ],
            }
        }
    }
    assert not {"hooks", "settings", "userConfig", "skills"}.intersection(manifest)
    assert sorted(path.name for path in BUNDLE.iterdir()) == [
        ".claude-plugin",
        ".mcp.json",
        "README.md",
    ]


def test_plugin_documents_local_execution_and_privacy():
    readme = (BUNDLE / "README.md").read_text()
    assert "## Privacy Policy" in readme
    assert "https://github.com/chriswu727/argus/blob/main/PRIVACY.md" in readme
    assert "They do not execute in claude.ai web chat" in readme
    assert "does not mean it has been accepted" in readme


@pytest.mark.asyncio
async def test_plugin_launch_command_handshake():
    manifest = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    config = json.loads((BUNDLE / ".mcp.json").read_text())["mcpServers"]["argus"]
    async with stdio_client(StdioServerParameters(**config)) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "argus"
            assert initialized.serverInfo.version == manifest["version"]
            tools = (await client.list_tools()).tools
            assert len(tools) == 28
            assert {"start_session", "regression_check", "get_errors"} <= {
                tool.name for tool in tools
            }
            assert not any(tool.name.startswith("screen_") for tool in tools)
            result = await client.call_tool("observe", {})
            assert result.isError
            assert result.structuredContent["error_code"] == "no_session"
