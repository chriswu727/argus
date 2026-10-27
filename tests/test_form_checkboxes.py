"""test_form sets checkboxes; text checks accept punctuation-edged targets."""

from __future__ import annotations

import argus.mcp_server as m
from argus.application.text import _token_present

from .test_reproduction_receipt import _end, _session_on_page

_FORM = """<html><body>
<form onsubmit="event.preventDefault();
  document.getElementById('out').textContent =
    'agree=' + document.getElementById('agree').checked +
    ' news=' + document.getElementById('news').checked;">
  <label>Name <input name="name"></label>
  <label><input type="checkbox" id="agree"> I agree to the terms</label>
  <label><input type="checkbox" id="news" checked> Send me news</label>
  <button type="submit">Save</button>
</form>
<p id="out"></p>
</body></html>"""


async def test_test_form_checks_and_unchecks_checkboxes():
    # Models pass booleans for checkboxes; filling them raised
    # "Input of type checkbox cannot be filled" and skipped the field.
    await _session_on_page(_FORM)
    try:
        fn = getattr(m.test_form, "fn", m.test_form)
        out = await fn(
            {"Name": "Alex", "I agree to the terms": True, "Send me news": "false"}, "Save"
        )
        assert "[FAIL]" not in out
        assert "set to checked" in out and "set to unchecked" in out
        state = await m.runtime.session.browser.get_state()
        assert "agree=true news=false" in state.page_text
    finally:
        await _end()


def test_token_boundaries_apply_only_to_word_edges():
    # "-50%" printed straight after a price ("$89.99-50%") is a real occurrence.
    assert _token_present("-50%", "$89.99-50%$89.99")
    assert _token_present("$5 off", "save $5 off today")
    # Word-edged targets still need boundaries.
    assert not _token_present("cat", "category")
    assert not _token_present("Delete", "Deleted")
    assert not _token_present("$5", "$50")


_RADIOS = """<html><body>
<form onsubmit="event.preventDefault();
  document.getElementById('out').textContent =
    'size=' + document.querySelector('input[name=size]:checked').value;">
  <label><input type="radio" name="size" value="small" checked> Small</label>
  <label><input type="radio" name="size" value="large"> Large</label>
  <button type="submit">Save</button>
</form>
<p id="out"></p>
</body></html>"""


async def test_test_form_sets_the_resolved_option_of_a_shared_name():
    # Radios share a name, so a name selector matches the first option; the
    # resolved option (Large) must be the one that changes.
    await _session_on_page(_RADIOS)
    try:
        fn = getattr(m.test_form, "fn", m.test_form)
        out = await fn({"Large": True}, "Save")
        assert "[FAIL]" not in out
        state = await m.runtime.session.browser.get_state()
        assert "size=large" in state.page_text
    finally:
        await _end()


_PERSISTED = """<html><body>
<label><input type="checkbox" id="keep"> Keep me signed in</label>
<script>
  const box = document.getElementById('keep');
  box.checked = localStorage.getItem('keep') === '1';
  box.addEventListener('change', () => localStorage.setItem('keep', box.checked ? '1' : '0'));
</script>
</body></html>"""


async def test_checkbox_replay_applies_the_state_instead_of_toggling():
    # Replays start from a copy of the live storage, where the box is already
    # checked; a recorded click would uncheck it.
    await _session_on_page(_PERSISTED)
    try:
        s = m.runtime.session
        fn = getattr(m.test_form, "fn", m.test_form)
        await fn({"Keep me signed in": True}, "enter")
        action = next(a for a in s.action_trace if a["description"] == "Keep me signed in")
        assert action["tool"] == "set_checked" and action["value"] == "true"
        result = await s.browser.replay(s.browser.current_url, [action])
        assert not result["diverged"]
        boxes = [el for el in result["final_state"].elements if el.type == "checkbox"]
        assert boxes and boxes[0].checked is True
    finally:
        await _end()
