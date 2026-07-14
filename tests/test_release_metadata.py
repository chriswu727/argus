import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_stays_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    server = json.loads((ROOT / "server.json").read_text())
    version = project["project"]["version"]

    assert server["name"] == "io.github.chriswu727/argus"
    assert server["version"] == version
    assert server["packages"][0]["identifier"] == project["project"]["name"]
    assert server["packages"][0]["version"] == version
    assert project["project"]["scripts"]["argus-testing"] == "argus.mcp_server:main"
    assert f"mcp-name: {server['name']}" in (ROOT / "README.md").read_text()
