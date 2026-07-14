"""The exported MCP surface is a public contract, not an implementation detail."""
from __future__ import annotations

from pathlib import Path

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

    import json

    report_json = next(output_dir.glob("report_*.json"))
    payload = json.loads(report_json.read_text())
    assert payload["review_mode"] == "visual"
    assert payload["tool_calls"] == 4
    assert payload["observations"][0]["title"] == "Heading feels visually crowded"
