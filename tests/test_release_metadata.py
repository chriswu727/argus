import json
import re
from pathlib import Path

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
