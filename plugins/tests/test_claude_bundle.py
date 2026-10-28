from __future__ import annotations

import json
import os
import shutil
import subprocess
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
                    "--isolated",
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
    runtime = next(package for package in lock["package"] if package["name"] == "argus-testing")
    assert project["dependencies"] == [f"argus-testing=={runtime['version']}"]
    assert project["version"] == runtime["version"]
    assert runtime["source"] == {"registry": "https://pypi.org/simple"}
    assert all(wheel["hash"].startswith("sha256:") for wheel in runtime["wheels"])
    assert not {"hooks", "settings", "userConfig", "skills"}.intersection(manifest)
    assert sorted(path.name for path in BUNDLE.iterdir()) == [
        ".claude-plugin",
        ".mcp.json",
        "README.md",
        "pyproject.toml",
        "skills",
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
    lock = tomllib.loads((BUNDLE / "uv.lock").read_text())
    runtime = next(package for package in lock["package"] if package["name"] == "argus-testing")
    playwright = next(package for package in lock["package"] if package["name"] == "playwright")
    command = (
        f"uvx --python 3.12 --from argus-testing=={runtime['version']} "
        f"--with playwright=={playwright['version']} python -m playwright install chromium"
    )
    assert command in (BUNDLE / "README.md").read_text()


def test_skill_installs_the_browser_into_the_servers_locked_environment():
    skill = (BUNDLE / "skills" / "test" / "SKILL.md").read_text()
    server = json.loads((BUNDLE / ".mcp.json").read_text())["mcpServers"]["argus"]
    assert skill.startswith("---\ndescription: ")
    launcher = " ".join(server["args"][: server["args"].index("argus-mcp")])
    command = (
        "uv "
        + launcher.replace("${CLAUDE_PLUGIN_ROOT}", '"${CLAUDE_PLUGIN_ROOT}"')
        + " python -m playwright install chromium"
    )
    assert command in skill
    assert "/argus-testing:test" in (BUNDLE / "README.md").read_text()


def test_plugin_documents_local_execution_and_privacy():
    readme = (BUNDLE / "README.md").read_text()
    assert "## Privacy Policy" in readme
    assert "https://github.com/chriswu727/argus/blob/main/PRIVACY.md" in readme
    assert "They do not execute in claude.ai web chat" in readme
    assert "does not mean it has been accepted" in readme


@pytest.mark.asyncio
async def test_plugin_launch_command_handshake(tmp_path):
    project = tomllib.loads((BUNDLE / "pyproject.toml").read_text())["project"]
    config = json.loads((BUNDLE / ".mcp.json").read_text())["mcpServers"]["argus"]
    config["args"] = [
        argument.replace("${CLAUDE_PLUGIN_ROOT}", str(BUNDLE)) for argument in config["args"]
    ]
    for _ in range(2):
        # stdio clients pass a minimal environment; keep proxy and certificate
        # settings so uv can still reach package indexes behind a proxy.
        network = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _NETWORK_ENV or key.startswith("UV_")
        }
        config["env"] = {**network, "VIRTUAL_ENV": str(tmp_path / "host-venv")}
        config["cwd"] = str(tmp_path)
        async with stdio_client(StdioServerParameters(**config)) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "argus"
                assert initialized.serverInfo.version == project["version"]
                tools = (await client.list_tools()).tools
                assert len(tools) == 29
                assert {"start_session", "regression_check", "get_errors", "export_test"} <= {
                    tool.name for tool in tools
                }
                assert not any(tool.name.startswith("screen_") for tool in tools)
                result = await client.call_tool("observe", {})
                assert result.isError
                assert result.structuredContent["error_code"] == "no_session"
        assert not (tmp_path / "host-venv").exists()
        assert not (BUNDLE / ".venv").exists()


def test_launcher_is_isolated_and_cleans_up(tmp_path):
    server = json.loads((BUNDLE / ".mcp.json").read_text())["mcpServers"]["argus"]
    args = server["args"][: server["args"].index("argus-mcp")]
    args = [arg.replace("${CLAUDE_PLUGIN_ROOT}", str(BUNDLE)) for arg in args]
    probe = "import json, sys; print(json.dumps({'prefix': sys.prefix}))"
    result = subprocess.run(
        [server["command"], *args, "python", "-c", probe],
        env={
            **os.environ,
            "VIRTUAL_ENV": str(tmp_path / "host-venv"),
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "project-venv"),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    prefix = Path(json.loads(result.stdout)["prefix"])
    assert prefix not in {tmp_path / "host-venv", tmp_path / "project-venv", BUNDLE / ".venv"}
    assert not prefix.exists()
    assert not (tmp_path / "host-venv").exists()
    assert not (tmp_path / "project-venv").exists()
    assert not (BUNDLE / ".venv").exists()


def test_launcher_rejects_a_stale_lock_without_running_the_command(tmp_path):
    bundle = tmp_path / "plugin"
    shutil.copytree(BUNDLE, bundle)
    project = bundle / "pyproject.toml"
    project.write_text(
        project.read_text().replace(
            'name = "argus-testing-plugin-runtime"', 'name = "changed-plugin-runtime"'
        )
    )
    lock_before = (bundle / "uv.lock").read_bytes()
    server = json.loads((bundle / ".mcp.json").read_text())["mcpServers"]["argus"]
    args = server["args"][: server["args"].index("argus-mcp")]
    args = [arg.replace("${CLAUDE_PLUGIN_ROOT}", str(bundle)) for arg in args]
    marker = tmp_path / "command-ran"
    result = subprocess.run(
        [
            server["command"],
            *args,
            "--offline",
            "python",
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--locked" in result.stderr and "lockfile" in result.stderr
    assert not marker.exists()
    assert (bundle / "uv.lock").read_bytes() == lock_before
    assert not (bundle / ".venv").exists()
