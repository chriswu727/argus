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
    return json.loads(path.read_text(encoding="utf-8"))


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
    assert manifest["name"] == claude["name"]
    project = tomllib.loads((BUNDLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
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
    assert "[Codex plugin](plugins/codex)" in (ROOT / "README.md").read_text(encoding="utf-8")
    for readme in (BUNDLE / "README.md", ROOT / "docs/usage.md"):
        text = readme.read_text(encoding="utf-8")
        assert "codex plugin marketplace add chriswu727/argus" in text and install in text


def test_codex_skill_declares_itself_and_pins_the_locked_browser():
    skill = (BUNDLE / "skills" / "argus-test" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: argus-test\ndescription: ")
    manifest = load(BUNDLE / "plugin.json")
    lock = tomllib.loads((BUNDLE / "uv.lock").read_text(encoding="utf-8"))
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
        text = skill.read_text(encoding="utf-8")
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
        text = skill.read_text(encoding="utf-8")
        for tool in ("`screenshot`", "`check_layout`", "`record_observation`"):
            assert tool in text, (skill, tool)
        assert "use `record_bug` with `verify`\nonly when a text check" in text


SKILLS = (
    BUNDLE / "skills" / "argus-test" / "SKILL.md",
    CLAUDE / "skills" / "test" / "SKILL.md",
)


def _sections(skill):
    """Section title -> text, for the numbered sections of a skill."""
    parts = re.split(r"^## (\d\. .*)$", skill.read_text(encoding="utf-8"), flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_both_skills_give_the_same_procedure():
    codex, claude = (_sections(skill) for skill in SKILLS)
    assert list(codex) == list(claude) and len(codex) == 5
    for title in codex:
        # Only the setup commands differ: each plugin launches its runtime its own way.
        if not title.startswith("2."):
            assert codex[title] == claude[title], title


def test_both_skills_trigger_on_testing_finished_work_and_fixes():
    for skill in SKILLS:
        description = (
            skill.read_text(encoding="utf-8").split("description: ", 1)[1].split("\n", 1)[0]
        )
        for occasion in ("asks to test", "before the work is called done", "has been fixed"):
            assert occasion in description, (skill, occasion)


def test_both_skills_find_the_address_and_handle_setup_without_the_user():
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        scope = _sections(skill)["1. Settle the scope yourself"]
        assert "Ask only when none of this gives an address." in scope, skill
        assert "a login you were not\n  given" in scope, skill
        start = _sections(skill)["2. Start"]
        assert "The first session downloads the browser" in start, skill
        assert "call\n  `start_session` again" in start, skill
        for handed_back in ("ask for it", "after they agree", "offer to install"):
            assert handed_back not in text, (skill, handed_back)


def test_both_skills_say_how_to_recover_without_repeating_a_write():
    for skill in SKILLS:
        recover = _sections(skill)["3. Test"].split("### When something goes wrong", 1)[1]
        for step in (
            "Call `observe` and pick the target from what is there now.",
            "repeat the action only if it did not\n  take effect",
            "Call `end_session` so the report and\n  the journal",
            "Mark that goal `blocked`",
            "report it as inconclusive",
        ):
            assert step in recover, (skill, step)
        assert "A page with no defects is a normal\nresult" in skill.read_text(encoding="utf-8"), (
            skill
        )


def test_both_skills_report_evidence_and_what_was_not_finished():
    for skill in SKILLS:
        deliver = _sections(skill)["4. Deliver"]
        for part in (
            "What you tested",
            "where the evidence is",
            "What was not finished and why",
            "The paths of any test files you exported.",
        ):
            assert part in deliver, (skill, part)
        assert "Never present an observed finding as verified." in deliver, skill


def test_both_skills_export_a_requested_file_before_closing_the_session():
    """export_test reads the open session's findings, so a skill that closed
    the session first would send the agent into a no_session error."""
    for skill in SKILLS:
        deliver = " ".join(_sections(skill)["4. Deliver"].split())
        export = deliver.index("export it first, while the session is open")
        close = deliver.index("Then close with `end_session`")
        assert deliver.index("`export_test`") < close and export < close, skill
        assert "`end_session` closes it: after that the call fails" in deliver, skill
        # Every other mention of closing the session comes after the export step.
        assert deliver.count("close with `end_session`") == 1, skill


def test_both_skills_keep_exports_optional_and_private():
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        deliver = " ".join(_sections(skill)["4. Deliver"].split())
        assert deliver.startswith("**Only if the user asked for a regression test file**"), skill
        assert "Without such a request, skip this step" in deliver, skill
        assert "exporting is not part of every run" in deliver, skill
        assert "`run=true` only when the user authorized" in deliver, skill
        assert "only set `save_private_values=true` when the user explicitly" in deliver, skill
        assert "never be published or committed" in deliver, skill
        # Re-testing goes through the journal; nobody sets variables by hand for it.
        assert "ARGUS_VALUES_FILE" not in text and "ARGUS_EXPORT_ID" not in text, skill


def test_both_skills_retest_fixes_from_the_journal():
    for skill in SKILLS:
        retest = _sections(skill)["5. Re-test after a fix"]
        assert 'review_mode="regression"' in retest, skill
        assert retest.index("do that action once") < retest.index("Call `regression_check`"), skill
        assert "inconclusive with the reason" in retest, skill


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
                assert len(tools) == 29
                assert {"regression_check", "export_test"} <= tools
                result = await client.call_tool("observe", {})
                assert result.structuredContent["error_code"] == "no_session"


def test_both_skills_say_what_a_real_host_got_wrong():
    """Each line answers a failure seen when a small model ran the skill for
    real: it asked instead of looking, passed the expected state as the
    symptom, and started fixing before it had reported."""
    for skill in SKILLS:
        text = " ".join(skill.read_text(encoding="utf-8").split())
        description = (
            skill.read_text(encoding="utf-8").split("description: ", 1)[1].split("\n", 1)[0]
        )
        assert "Load it before asking the user where the app is" in description, skill
        assert "Do not open by asking where the app is or what to test." in text, skill
        assert "never before you have closed the session and reported" in text, skill
        assert "`record_bug`'s `verify` takes the symptom" in text, skill
        assert "Finding a bug is not a reason to stop or to start fixing." in text, skill


def test_both_skills_keep_environment_files_out_of_the_conversation():
    """A real host ran `cat .env` to find the app's address. The skills now
    send it to the README and start scripts first, and only to one host or
    port variable if nothing else gives the address."""
    for skill in SKILLS:
        scope = " ".join(_sections(skill)["1. Settle the scope yourself"].split())
        assert "`.env`)" not in scope and "README, `.env`" not in scope, skill
        assert "do not print or read one whole" in scope, skill
        assert "leave every other value out of the conversation" in scope, skill
        assert scope.index("the README") < scope.index("Environment files"), skill
