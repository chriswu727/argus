import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_stays_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    server = json.loads((ROOT / "server.json").read_text())
    glama = json.loads((ROOT / "glama.json").read_text())
    version = project["project"]["version"]

    assert server["name"] == "io.github.chriswu727/argus"
    assert server["version"] == version
    assert server["packages"][0]["identifier"] == project["project"]["name"]
    assert server["packages"][0]["version"] == version
    assert project["project"]["scripts"]["argus-testing"] == "argus.mcp_server:main"
    assert project["project"]["urls"]["Homepage"] == server["websiteUrl"]
    assert "testing engineer" in project["project"]["description"].lower()
    assert len(server["description"]) <= 100
    assert all(term in server["description"].lower() for term in ("qa", "mcp", "web", "macos", "bugs", "verifies"))
    assert f"mcp-name: {server['name']}" in (ROOT / "README.md").read_text()
    assert "chriswu727" in glama["maintainers"]


def test_readme_covers_every_public_tool_profile():
    from argus.mcp_server import _CORE_TOOL_NAMES, _SCREEN_TOOL_NAMES, mcp

    readme = (ROOT / "README.md").read_text()
    documented_tools = set(re.findall(r"`([a-z][a-z0-9_]*)`", readme))
    full_tools = set(mcp._tool_manager._tools)

    assert full_tools <= documented_tools
    assert re.search(rf"\| `core` \| {len(_CORE_TOOL_NAMES)} \|", readme)
    assert re.search(rf"\| `screen` \| {len(_SCREEN_TOOL_NAMES)} \|", readme)
    assert re.search(rf"\| `full` \| {len(full_tools)} \|", readme)
    assert "uvx --from argus-testing argus-mcp" in readme


def test_client_install_examples_start_the_published_stdio_server():
    readme = (ROOT / "README.md").read_text()
    agent_install = (ROOT / "llms-install.md").read_text()
    example = json.loads((ROOT / "examples" / "mcp-config.json").read_text())
    expected = {"command": "uvx", "args": ["--from", "argus-testing", "argus-mcp"]}

    assert example["mcpServers"]["argus"] == expected
    assert "claude mcp add argus -- uvx --from argus-testing argus-mcp" in readme
    assert "codex mcp add argus -- uvx --from argus-testing argus-mcp" in readme
    assert "claude mcp add argus -- uvx --from argus-testing argus-mcp" in agent_install
    assert "codex mcp add argus -- uvx --from argus-testing argus-mcp" in agent_install
    assert "core` profile with 30 tools" in agent_install

    cursor_url = re.search(r"https://cursor\.com/install-mcp\?name=argus&config=[^)]+", readme)
    assert cursor_url is not None
    encoded = parse_qs(urlparse(cursor_url.group()).query)["config"][0]
    assert json.loads(base64.b64decode(encoded)) == expected


def test_every_mcp_tool_has_display_and_risk_metadata():
    from argus.mcp_server import (
        _DESTRUCTIVE_TOOL_NAMES,
        _READ_ONLY_TOOL_NAMES,
        _TOOL_TITLES,
        mcp,
    )

    assert set(_TOOL_TITLES) == set(mcp._tool_manager._tools)
    for name, tool in mcp._tool_manager._tools.items():
        assert tool.title, name
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is (name in _READ_ONLY_TOOL_NAMES), name
        assert tool.annotations.destructiveHint is (name in _DESTRUCTIVE_TOOL_NAMES), name
        assert tool.annotations.idempotentHint is (name in _READ_ONLY_TOOL_NAMES), name
