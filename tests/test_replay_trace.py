"""Structured action-trace capture (foundation for the replay-steps receipt),
and a lock that click_what now hits the resolved duplicate via the nth-aware
path (the F3 fix had only reached BrowserDriver.click, not the click_what tool).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import argus.mcp_server as m


def _furl(html: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    return Path(f.name).as_uri()


async def _call(tool, **kw):
    return await (getattr(tool, "fn", tool))(**kw)


async def _start(url):
    try:
        await _call(m.start_session, url=url)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


_DUP = """<html><body><ul id=list>
<li>Buy groceries <button>Delete</button></li>
<li>Pay rent <button>Delete</button></li>
<li>Walk dog <button>Delete</button></li>
</ul><script>document.querySelectorAll('#list button').forEach(
  b => b.onclick = () => b.closest('li').remove());</script></body></html>"""


async def test_click_what_hits_resolved_duplicate():
    await _start(_furl(_DUP))
    try:
        await _call(m.observe)
        await _call(m.click_what, description="Delete #2")
        remaining = await m._session.browser._page.eval_on_selector_all(
            "#list li", "ns => ns.map(n => n.textContent.trim().split(' Delete')[0])")
        assert remaining == ["Buy groceries", "Walk dog"]  # 2nd removed, not the 1st
    finally:
        await _call(m.end_session)


_FORM = """<html><body><input id=e placeholder="email"><button id=s>Save</button>
<script>document.getElementById('s').onclick=()=>{};</script></body></html>"""


async def test_action_trace_captured_onto_bug():
    await _start(_furl(_FORM))
    try:
        await _call(m.observe)
        await _call(m.type_into, description="email", text="alice@x.com")
        await _call(m.click_what, description="Save")
        await _call(m.record_bug, title="t", severity="low", evidence={"screenshot": "skip"})
        steps = m._session.bugs[-1].replay_steps
        assert [s["tool"] for s in steps] == ["type_into", "click_what"]
        assert steps[0]["value"] == "alice@x.com"
        assert steps[0]["description"] == "email"
    finally:
        await _call(m.end_session)
