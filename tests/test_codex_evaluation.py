from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from argus_lab.evaluation.agent_fixture import agent_fixture
from argus_lab.evaluation.browser_baseline import BrowserBaseline
from argus_lab.evaluation.codex_runner import clean_environment, command
from argus_lab.evaluation.gateway import CallBudget


def test_codex_command_disallows_keys_shell_web_plugins_and_uses_subscription(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("CODEX_API_KEY", "SYNTHETIC_SECRET")
    assert "SYNTHETIC_SECRET" not in json.dumps(clean_environment())
    args = command("fixed-model", "high", "http://127.0.0.1:1", "argus", tmp_path / "out", tmp_path)
    assert "--ignore-user-config" in args and "--ephemeral" in args
    assert 'forced_login_method="chatgpt"' in args
    assert "features.shell_tool=false" in args
    assert 'web_search="disabled"' in args
    assert "features.plugins=false" in args
    assert "project_doc_max_bytes=0" in args
    assert 'mcp_servers.testing.default_tools_approval_mode="approve"' in args
    assert "mcp_servers.testing.required=true" in args


def test_tool_budget_is_hard_bounded():
    budget = CallBudget(2)
    assert budget.consume() == 1
    assert budget.consume() == 2
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.consume()
    assert budget.used == 2


@pytest.mark.parametrize("family", ["profile", "tasks", "form", "cart"])
@pytest.mark.parametrize("variant", ["buggy", "fixed"])
async def test_agent_fixture_ground_truth_through_real_browser(family, variant):
    with agent_fixture(f"{family}-{variant}") as (origin, events):
        browser = BrowserBaseline(origin)
        await browser.start()
        try:
            page = browser.page
            await page.goto(origin)
            if family == "profile":
                await page.get_by_label("Display name", exact=True).fill("Morgan")
                await page.get_by_role("button", name="Save").click()
                assert "Display name: Morgan" in await page.inner_text("body")
                await page.reload()
                assert ("Display name: Morgan" in await page.inner_text("body")) == (
                    variant == "fixed"
                )
                assert [event["method"] for event in events] == ["GET", "POST", "GET", "GET"]
            elif family == "tasks":
                await page.get_by_role("button", name="Delete task").click()
                assert "No tasks" in await page.inner_text("body")
                await page.reload()
                assert ("No tasks" in await page.inner_text("body")) == (variant == "fixed")
            elif family == "form":
                for invalid in ("bad-address", "a@", ""):
                    await page.get_by_label("Email").fill(invalid)
                    await page.get_by_role("button", name="Subscribe").click()
                    assert ("Invalid email" in await page.inner_text("body")) == (
                        variant == "fixed"
                    )
                await page.get_by_label("Email").fill("qa@example.test")
                await page.get_by_role("button", name="Subscribe").click()
                assert "Subscribed" in await page.inner_text("body")
                await page.get_by_label("Email").fill("@example.com")
                await page.get_by_role("button", name="Subscribe").click()
                assert "Subscribed" in await page.inner_text("body")
            else:
                assert "Subtotal: $24" in await page.inner_text("body")
                await page.get_by_role("link", name="Checkout", exact=True).click()
                assert ("Subtotal: $24" in await page.inner_text("body")) == (variant == "fixed")
        finally:
            await browser.close()


@pytest.mark.parametrize("arm", ["baseline", "argus"])
async def test_evaluation_gateway_real_stdio_without_model(tmp_path, arm):
    with agent_fixture("profile-buggy") as (origin, events):
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "argus_lab.evaluation.gateway",
                "--arm",
                arm,
                "--origin",
                origin,
                "--output",
                str(tmp_path),
                "--limit",
                "5",
            ],
            env={key: os.environ[key] for key in ("HOME", "PATH") if key in os.environ},
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                progress = []

                async def callback(value, total, message):
                    progress.append(message)

                if arm == "argus":
                    started = await client.call_tool(
                        "start_session",
                        {"url": origin, "goals": ["edit-name", "reload-name"]},
                        progress_callback=callback,
                    )
                    assert not started.isError
                    assert started.structuredContent["data"]["protocol"]
                    assert any("goals exercised" in message for message in progress)
                    await client.call_tool(
                        "type_into", {"description": "Display name", "text": "Morgan"}
                    )
                    saved = await client.call_tool("click_what", {"description": "Save"})
                    assert "Morgan" in saved.structuredContent["data"]["visible_text"]
                    checked = await client.call_tool(
                        "verify_persistence", {"expect": "present", "target_text": "Morgan"}
                    )
                    assert checked.structuredContent["data"]["verification"]["matches"] is False
                    assert (
                        "Display name: Alex"
                        in checked.structuredContent["data"]["observation"]["visible_text"]
                    )
                    await client.call_tool("end_session", {})
                else:
                    assert not (await client.call_tool("browser_navigate", {"url": origin})).isError
                    await client.call_tool(
                        "browser_fill", {"label": "Display name", "value": "Morgan"}
                    )
                    saved = await client.call_tool(
                        "browser_click", {"role": "button", "name": "Save"}
                    )
                    assert "Morgan" in saved.content[0].text
                    checked = await client.call_tool("browser_navigate", {"url": origin})
                    assert "Morgan" not in checked.content[0].text
                    await client.call_tool("browser_snapshot", {})
                exhausted = await client.call_tool(
                    "observe" if arm == "argus" else "browser_snapshot", {}
                )
                assert exhausted.isError and "exhausted" in exhausted.content[0].text
        assert any(event["method"] == "POST" for event in events)
    assert len((tmp_path / "tools.jsonl").read_text().splitlines()) == 5
