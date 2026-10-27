from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "plugins" / "codex"
CLAUDE = ROOT / "plugins" / "claude"
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


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_manifest_and_server_follow_the_agent_plugins_schema():
    manifest = load(BUNDLE / "plugin.json")
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", manifest["name"])
    assert set(manifest) <= {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
    config = load(BUNDLE / "mcp.json")
    assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert config["mcpServers"] == {
        "argus": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--project",
                ".",
                "--locked",
                "--python",
                "3.12",
                "argus-mcp",
                "--tool-profile",
                "web",
            ],
            "cwd": "${PLUGIN_ROOT}",
        }
    }


def test_codex_and_claude_bundles_pin_the_same_runtime():
    assert (BUNDLE / "pyproject.toml").read_bytes() == (CLAUDE / "pyproject.toml").read_bytes()
    assert (BUNDLE / "uv.lock").read_bytes() == (CLAUDE / "uv.lock").read_bytes()
    manifest = load(BUNDLE / "plugin.json")
    claude = load(CLAUDE / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == claude["name"] and manifest["version"] == claude["version"]
    project = tomllib.loads((BUNDLE / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == [f"argus-testing=={manifest['version']}"]


def test_codex_marketplace_lists_this_bundle_and_docs_show_install():
    marketplace = load(ROOT / ".agents" / "plugins" / "marketplace.json")
    (entry,) = marketplace["plugins"]
    assert entry == {
        "name": load(BUNDLE / "plugin.json")["name"],
        "source": {"source": "local", "path": "./plugins/codex"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
        "category": "Developer Tools",
    }
    install = f"codex plugin add {entry['name']}@{marketplace['name']}"
    for readme in (BUNDLE / "README.md", ROOT / "README.md"):
        text = readme.read_text()
        assert "codex plugin marketplace add chriswu727/argus" in text and install in text


def test_codex_skill_declares_itself_and_pins_the_locked_browser():
    skill = (BUNDLE / "skills" / "argus-test" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: argus-test\ndescription: ")
    manifest = load(BUNDLE / "plugin.json")
    lock = tomllib.loads((BUNDLE / "uv.lock").read_text())
    playwright = next(package for package in lock["package"] if package["name"] == "playwright")
    assert (
        f"uvx --python 3.12 --from argus-testing=={manifest['version']} "
        f"--with playwright=={playwright['version']} python -m playwright install chromium"
    ) in skill


def test_both_skills_give_valid_test_action_expectations():
    from argus.application.expectations import _validated_expectation

    for skill in (
        BUNDLE / "skills" / "argus-test" / "SKILL.md",
        CLAUDE / "skills" / "test" / "SKILL.md",
    ):
        text = skill.read_text()
        assert "`test_action` with an `expect` prediction" in text
        examples = [json.loads(m) for m in re.findall(r"`(\{.*?\})`", text)]
        assert examples
        for example in examples:
            assert _validated_expectation(example)[1] == [], (skill, example)


def test_both_skills_ask_for_a_visual_review_kept_as_observations():
    for skill in (
        BUNDLE / "skills" / "argus-test" / "SKILL.md",
        CLAUDE / "skills" / "test" / "SKILL.md",
    ):
        text = skill.read_text()
        for tool in ("`screenshot`", "`check_layout`", "`record_observation`"):
            assert tool in text, (skill, tool)
        assert "use `record_bug` with `verify`\nonly when a text check" in text


@pytest.mark.asyncio
async def test_codex_launch_command_handshake_from_plugin_root():
    manifest = load(BUNDLE / "plugin.json")
    config = load(BUNDLE / "mcp.json")["mcpServers"]["argus"]
    network = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _NETWORK_ENV or key.startswith("UV_")
    }
    with tempfile.TemporaryDirectory(prefix="argus-codex-check-") as temporary:
        params = StdioServerParameters(
            command=config["command"],
            args=config["args"],
            cwd=config["cwd"].replace("${PLUGIN_ROOT}", str(BUNDLE)),
            env={
                **network,
                "PLUGIN_ROOT": str(BUNDLE),
                "UV_PROJECT_ENVIRONMENT": str(Path(temporary) / "venv"),
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.version == manifest["version"]
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert len(tools) == 28 and "regression_check" in tools
                result = await client.call_tool("observe", {})
                assert result.structuredContent["error_code"] == "no_session"
