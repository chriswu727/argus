from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
_NETWORK_ENV = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
}
BUNDLE = ROOT / "plugins" / "claude"


def test_plugin_pins_the_published_runtime_and_safe_profile():
    manifest = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    project = tomllib.loads((BUNDLE / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((BUNDLE / "uv.lock").read_text())
    config = json.loads((BUNDLE / ".mcp.json").read_text())
    assert manifest["name"] == "argus-testing"
    assert manifest["license"] == "MIT"
    assert config == {
        "mcpServers": {
            "argus": {
                "command": "uv",
                "args": [
                    "run",
                    "--project",
                    "${CLAUDE_PLUGIN_ROOT}",
                    "--locked",
                    "--python",
                    "3.12",
                    "argus-mcp",
                    "--tool-profile",
                    "web",
                ],
            }
        }
    }
    assert project["dependencies"] == [f"argus-testing=={manifest['version']}"]
    runtime = next(package for package in lock["package"] if package["name"] == "argus-testing")
    assert runtime["version"] == manifest["version"]
    assert runtime["source"] == {"registry": "https://pypi.org/simple"}
    assert all(wheel["hash"].startswith("sha256:") for wheel in runtime["wheels"])
    assert not {"hooks", "settings", "userConfig", "skills"}.intersection(manifest)
    assert sorted(path.name for path in BUNDLE.iterdir()) == [
        ".claude-plugin",
        ".mcp.json",
        "README.md",
        "pyproject.toml",
        "uv.lock",
    ]


def test_marketplace_lists_this_bundle():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["source"] == "./plugins/claude"
    install = f"/plugin install {manifest['name']} --marketplace chriswu727/argus"
    assert install in (BUNDLE / "README.md").read_text()
    assert install in (ROOT / "README.md").read_text()


def test_warm_up_command_matches_the_lock():
    manifest = json.loads((BUNDLE / ".claude-plugin" / "plugin.json").read_text())
    lock = tomllib.loads((BUNDLE / "uv.lock").read_text())
    playwright = next(package for package in lock["package"] if package["name"] == "playwright")
    command = (
        f"uvx --python 3.12 --from argus-testing=={manifest['version']} "
        f"--with playwright=={playwright['version']} python -m playwright install chromium"
    )
    assert command in (BUNDLE / "README.md").read_text()


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
    config["args"] = [
        argument.replace("${CLAUDE_PLUGIN_ROOT}", str(BUNDLE)) for argument in config["args"]
    ]
    with tempfile.TemporaryDirectory(prefix="argus-plugin-check-") as temporary:
        # stdio clients pass a minimal environment; keep proxy and certificate
        # settings so uv can still reach package indexes behind a proxy.
        network = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _NETWORK_ENV or key.startswith("UV_")
        }
        config["env"] = {**network, "UV_PROJECT_ENVIRONMENT": str(Path(temporary) / "venv")}
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
