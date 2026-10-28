import base64
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_stays_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    server = json.loads((ROOT / "server.json").read_text())
    glama = json.loads((ROOT / "glama.json").read_text())
    version = project["project"]["version"]

    assert project["project"]["requires-python"] == ">=3.11"
    assert "Pillow>=13.0.0" in project["project"]["dependencies"]
    assert "filelock>=4.2.4,<5" in project["project"]["dependencies"]

    assert server["name"] == "io.github.chriswu727/argus"
    assert server["version"] == version
    assert server["packages"][0]["identifier"] == project["project"]["name"]
    assert server["packages"][0]["version"] == version
    assert project["project"]["scripts"]["argus-testing"] == "argus.mcp_server:main"
    assert project["project"]["urls"]["Homepage"] == server["websiteUrl"]
    assert all(
        term in project["project"]["description"].lower()
        for term in ("testing mcp", "coding agents", "evidence", "persistence")
    )
    assert len(server["description"]) <= 100
    assert all(
        term in server["description"].lower()
        for term in ("testing mcp", "coding agents", "bugs", "evidence")
    )
    assert f"mcp-name: {server['name']}" in (ROOT / "README.md").read_text()
    assert "chriswu727" in glama["maintainers"]
    from argus import __version__
    from argus.bootstrap import create_runtime
    from argus.entrypoints.mcp.server import create_server

    assert __version__ == version
    assert (
        create_server(create_runtime())._mcp_server.create_initialization_options().server_version
        == version
    )


def test_readme_covers_every_public_tool_profile():
    from argus.entrypoints.mcp.server import tool_names
    from argus.mcp_server import mcp

    readme = (ROOT / "README.md").read_text()
    documented_tools = set(re.findall(r"`([a-z][a-z0-9_]*)`", readme))
    full_tools = set(mcp._tool_manager._tools)

    assert full_tools <= documented_tools
    for profile in ("web", "advanced", "screen", "full"):
        assert re.search(rf"\| `{profile}` \| {len(tool_names(profile))} \|", readme), profile
    assert "uvx --from argus-testing argus-mcp" in readme


def test_release_documentation_and_translation_are_current():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    readme = (ROOT / "README.md").read_text()
    translation = (ROOT / "README.zh-CN.md").read_text()
    assert f"What's new in {version}" in readme
    assert f"**{version}**" in translation
    assert f"## {version} - " in (ROOT / "CHANGELOG.md").read_text()
    overhead = json.loads((ROOT / "examples/response-overhead.json").read_text())
    assert overhead["argus_version"] == version
    measured_reduction = f"{overhead['aggregate']['reduction_percent']:.2f}%"
    assert measured_reduction in readme and measured_reduction in translation
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        "README.zh-CN.md" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    )
    assert "README.zh-CN.md" in readme and "README.md" in translation
    assert "uvx --from argus-testing argus-mcp" in translation
    assert re.search(r"[\u3400-\u9fff]", translation)
    for path in (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "PRIVACY.md",
        "SECURITY.md",
        "llms-install.md",
        "docs/architecture.md",
        "docs/agent-protocol.md",
        "docs/decisions.md",
        "docs/evaluation.md",
    ):
        content = (ROOT / path).read_text()
        assert not re.search(r"[\u3400-\u9fff]", content), path
    for path in ("README.md", "AGENTS.md", "docs/agent-protocol.md"):
        content = (ROOT / path).read_text().lower()
        assert "development main" not in content and "unreleased" not in content


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
    assert "web` profile with 29 tools" in agent_install

    cursor_url = re.search(r"https://cursor\.com/install-mcp\?name=argus&config=[^)]+", readme)
    assert cursor_url is not None
    encoded = parse_qs(urlparse(cursor_url.group()).query)["config"][0]
    assert json.loads(base64.b64decode(encoded)) == expected


def test_docker_image_installs_only_chromium_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "playwright install --with-deps chromium" in dockerfile
    assert "playwright install-deps" not in dockerfile


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
