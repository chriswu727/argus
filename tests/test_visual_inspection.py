from __future__ import annotations

import argus.mcp_server as m


async def test_visible_text_inspection_and_safe_layout_signals(tmp_path, monkeypatch):
    page = tmp_path / "visual.html"
    page.write_text(
        """
        <style>
          #overflow { position: absolute; left: 420px; width: 120px; }
          #clip { width: 45px; overflow: hidden; white-space: nowrap; }
          #tiny { width: 20px; height: 20px; padding: 0; }
        </style>
        <main>
          <h1>Yichen Wu</h1>
          <p>AI + FULL-STACK ENGINEER</p>
          <div id="overflow">Outside viewport</div>
          <div id="clip">This sentence is visibly clipped</div>
          <button id="tiny">Go</button>
        </main>
        <footer>© Yichen Wu <a href="#profile">LinkedIn</a></footer>
        """
    )
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path / "reports"))
    start = getattr(m.start_session, "fn", m.start_session)
    inspect = getattr(m.inspect_element, "fn", m.inspect_element)
    layout = getattr(m.check_layout, "fn", m.check_layout)
    end = getattr(m.end_session, "fn", m.end_session)

    try:
        await start(page.as_uri(), viewport_width=320, viewport_height=500)
        inspected = await inspect("Yichen Wu main heading")
        assert "resolved to <h1> via visible DOM" in inspected
        assert "LinkedIn" not in inspected

        signals = await layout()
        assert "Horizontal overflow: 1" in signals
        assert "Outside viewport" in signals
        assert "Clipped text: 1" in signals
        assert "This sentence is visibly clipped" in signals
        assert "Targets below 44px" in signals and "'Go'" in signals
    finally:
        if m._session.active:
            await end()
