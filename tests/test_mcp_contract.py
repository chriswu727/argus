"""The exported MCP surface is a public contract, not an implementation detail."""
from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

import argus.mcp_server as m


async def test_core_observation_tool_is_exported_and_helpers_are_private():
    names = {tool.name for tool in await m.mcp.list_tools()}

    assert "observe" in names
    assert "screen_observe" in names
    assert not {name for name in names if name.startswith("_")}


async def test_start_returns_observation_and_screenshot_returns_mcp_image(tmp_path, monkeypatch):
    page = tmp_path / "page.html"
    page.write_text("<html><body><h1>Contract page</h1><button>Save</button></body></html>")
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path / "reports"))

    start = getattr(m.start_session, "fn", m.start_session)
    end = getattr(m.end_session, "fn", m.end_session)
    try:
        output = await start(page.as_uri())
        assert "Initial observation" in output
        assert 'button "Save"' in output

        content = await m.mcp._tool_manager.call_tool(
            "screenshot", {"name": "contract"}, convert_result=True
        )
        assert [block.type for block in content] == ["text", "image"]
        screenshot_path = content[0].text.split(": ", 1)[1]
        assert Path(screenshot_path).is_absolute()
        assert Path(screenshot_path).exists()
    finally:
        if m._session.active:
            await end()


async def test_core_tool_profile_is_small_and_keeps_the_primary_workflow():
    original = dict(m.mcp._tool_manager._tools)
    try:
        before, after = m._apply_tool_profile("core")
        names = {tool.name for tool in await m.mcp.list_tools()}
        assert before > after
        assert after <= 30
        assert {"start_session", "observe", "check_layout", "screenshot", "end_session"} <= names
        assert "eval_js" not in names
        assert "screen_click_at" not in names
    finally:
        m.mcp._tool_manager._tools.clear()
        m.mcp._tool_manager._tools.update(original)


def test_core_mcp_context_has_a_regression_budget():
    tools = [m.mcp._tool_manager._tools[name] for name in m._CORE_TOOL_NAMES]
    footprint = sum(
        len(m.mcp.instructions)
        + len(tool.description or "")
        + len(json.dumps(tool.parameters, sort_keys=True))
        for tool in tools
    )

    assert len(m.mcp.instructions) <= 240
    assert footprint <= 35_000


def test_coverage_ledger_accumulates_discovered_pages():
    session = m.Session()
    session.url = "https://example.test/"
    session.pages_visited = ["https://example.test/", "https://example.test/account"]
    m._update_coverage_from_state(
        session,
        SimpleNamespace(
            url="https://example.test/",
            links=[{"href": "/account", "isInternal": True}],
        ),
    )
    m._update_coverage_from_state(
        session,
        SimpleNamespace(
            url="https://example.test/account",
            links=[{"href": "/settings", "isInternal": True}],
        ),
    )

    pages = m._coverage_snapshot(session, elapsed_seconds=0)["pages"]
    assert pages["discovered"] == ["/", "/account", "/settings"]
    assert pages["unvisited"] == ["/settings"]


def test_coverage_action_references_omit_input_values():
    session = m.Session()
    session.url = "https://example.test/login"
    m._record_action(session, "type_into", "Password field", "secret-value")
    m._record_action(session, "paste_into", "secret-value into token")

    refs = m._coverage_evidence_refs(session, m.Session()._coverage_evidence_cursor)
    serialized = json.dumps(refs)
    assert "Password field" in serialized
    assert "secret-value" not in serialized


async def test_observations_and_tool_calls_are_reported(tmp_path, monkeypatch):
    page = tmp_path / "review.html"
    page.write_text("<html><body><h1>Review</h1></body></html>")
    output_dir = tmp_path / "reports"
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(output_dir))

    start = getattr(m.start_session, "fn", m.start_session)
    observe = getattr(m.observe, "fn", m.observe)
    record = getattr(m.record_observation, "fn", m.record_observation)
    end = getattr(m.end_session, "fn", m.end_session)
    await start(page.as_uri(), review_mode="visual")
    await observe()
    recorded = await record(
        "Heading feels visually crowded",
        "The heading touches the viewport edge.",
        screenshot="skip",
    )
    ended = await end()

    assert "Recorded visual observation" in recorded
    assert "Tool calls: 4" in ended
    assert "Observations: 1" in ended

    report_json = next(output_dir.glob("report_*.json"))
    payload = json.loads(report_json.read_text())
    assert payload["review_mode"] == "visual"
    assert payload["tool_calls"] == 4
    assert payload["observations"][0]["title"] == "Heading feels visually crowded"


async def test_session_contract_requires_evidence_and_reaches_reports(tmp_path, monkeypatch):
    page = tmp_path / "contract.html"
    page.write_text(
        '<html><body><h1>Account</h1><a href="settings.html">Settings</a></body></html>'
    )
    (tmp_path / "settings.html").write_text("<html><body><h1>Settings</h1></body></html>")
    output_dir = tmp_path / "reports"
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(output_dir))

    start = getattr(m.start_session, "fn", m.start_session)
    update = getattr(m.coverage_update, "fn", m.coverage_update)
    click = getattr(m.click_what, "fn", m.click_what)
    capture = getattr(m.screenshot, "fn", m.screenshot)
    verify = getattr(m.verify_persistence, "fn", m.verify_persistence)
    record = getattr(m.record_observation, "fn", m.record_observation)
    end = getattr(m.end_session, "fn", m.end_session)
    try:
        started = await start(
            page.as_uri(),
            goals=["Change the account settings and verify persistence"],
            constraints=["Do not submit external forms"],
            time_budget_minutes=5,
        )
        assert "Session protocol (returned once" in started
        assert "Mark a goal in_progress before its journey" in started
        assert "Change the account settings" in started
        assert "Do not submit external forms" in started

        refused = await update("account settings", "exercised")
        assert "requires concrete evidence" in refused
        await update("account settings", "in_progress")
        clicked = await click("Settings")
        assert "settings.html" in clicked
        await capture("settings-goal")
        checked = await verify("present", "Settings")
        assert "Result: MATCH" in checked
        await record(
            "Settings page is understandable",
            "The settings heading is visible after navigation.",
            category="usability",
            screenshot="skip",
        )
        updated = await update(
            "account settings",
            "exercised",
            "Settings value remained visible after a fresh load.",
        )
        assert "1/1 exercised" in updated
        assert "Evidence linked:" in updated
        ended = await end()
        assert "Goals exercised: 1/1" in ended
    finally:
        if m._session.active:
            await end()

    payload = json.loads(next(output_dir.glob("report_*.json")).read_text())
    assert payload["constraints"] == ["Do not submit external forms"]
    goal = payload["coverage"]["goals"][0]
    assert goal["goal"] == "Change the account settings and verify persistence"
    assert goal["status"] == "exercised"
    assert goal["evidence"] == "Settings value remained visible after a fresh load."
    refs = goal["evidence_refs"]
    assert any(url.endswith("settings.html") for url in refs["urls"])
    assert refs["actions"] == [{"tool": "click_what", "description": "Settings"}]
    assert refs["screenshots"][0]["name"].endswith("settings-goal")
    assert refs["verifications"][0]["matches"] is True
    assert refs["findings"][0]["title"] == "Settings page is understandable"
    assert payload["coverage"]["time_budget"]["minutes"] == 5
    assert any(
        path.endswith("settings.html")
        for path in payload["coverage"]["pages"]["visited"]
    )
    report_html = next(output_dir.glob("report_*.html")).read_text()
    assert "Coverage Contract" in report_html
    assert "Settings value remained visible after a fresh load." in report_html
    assert "Tested URLs" in report_html
    assert "Verification" in report_html
    assert "Settings page is understandable" in report_html
