"""Interactions real apps need: hover-revealed controls, double-click editors
and confirm dialogs (found running the test skill on TodoMVC and flaskr)."""

from __future__ import annotations

import argus.mcp_server as m

from .test_reproduction_receipt import _end, _session_on_page

_TODOS = """<html><head><style>
  li .destroy { display: none; }
  li:hover .destroy { display: inline; }
  li.editing label { display: none; }
</style></head><body><ul>
  <li><input class="toggle" type="checkbox"><label>Walk dog</label>
      <button class="destroy" aria-label="Delete"></button></li>
  <li><input class="toggle" type="checkbox"><label>Buy milk</label>
      <button class="destroy" aria-label="Delete"></button></li>
</ul>
<script>
  document.querySelectorAll('li').forEach(li => {
    li.querySelector('.destroy').onclick = () => li.remove();
    li.querySelector('label').ondblclick = () => {
      li.classList.add('editing');
      const input = document.createElement('input');
      input.className = 'edit';
      input.value = li.querySelector('label').textContent;
      li.appendChild(input);
    };
  });
</script></body></html>"""

_CONFIRM = """<html><body>
<p id="post">Hello world</p>
<button onclick="if (confirm('Are you sure?')) document.getElementById('post').remove()">
  Delete</button>
<button onclick="document.title = 'clicked'">Other</button>
</body></html>"""


def _fn(name):
    tool = getattr(m, name)
    return getattr(tool, "fn", tool)


async def test_hover_reveals_a_control_that_can_then_be_clicked():
    await _session_on_page(_TODOS)
    try:
        hovered = await _fn("hover_what")("checkbox [unchecked] (near: 'Buy milk')")
        targets = [t["description"] for t in hovered.data["targets"]]
        delete = next(t for t in targets if t.startswith('button "Delete"') and "Buy milk" in t)
        await _fn("click_what")(delete)
        state = await m.runtime.session.browser.get_state()
        assert "Buy milk" not in state.page_text and "Walk dog" in state.page_text
    finally:
        await _end()


async def test_double_click_reaches_a_plain_text_inline_editor():
    await _session_on_page(_TODOS)
    try:
        out = await _fn("click_what")("Walk dog", double_click=True)
        assert "Double-clicked" in out
        state = await m.runtime.session.browser.get_state()
        assert any(el.type == "text" and el.value == "Walk dog" for el in state.elements)
        assert m.runtime.session.action_trace[-1]["tool"] == "double_click"
    finally:
        await _end()


async def test_confirm_dialogs_are_reported_and_accepted_only_when_asked():
    await _session_on_page(_CONFIRM)
    try:
        dismissed = await _fn("click_what")("Delete button")
        assert "DIALOG: confirm 'Are you sure?' was dismissed" in dismissed
        assert "accept_dialog=true" in dismissed
        assert dismissed.data["dialogs"][0]["response"] == "auto-dismiss"
        state = await m.runtime.session.browser.get_state()
        assert "Hello world" in state.page_text

        # An accept that no dialog consumes must not leak to a later dialog.
        await _fn("click_what")("Other", accept_dialog=True)
        await _fn("click_what")("Delete button")
        state = await m.runtime.session.browser.get_state()
        assert "Hello world" in state.page_text

        accepted = await _fn("test_action")("Delete button", accept_dialog=True)
        assert "was accepted" in accepted
        state = await m.runtime.session.browser.get_state()
        assert "Hello world" not in state.page_text
    finally:
        await _end()


async def test_unlabeled_editor_field_can_be_typed_into_and_submitted():
    # The edit input has no id/name/placeholder/aria-label; its name comes from
    # the neighbouring <label>. A selector built from that name matched nothing.
    await _session_on_page(_TODOS)
    try:
        await _fn("test_action")("Walk dog", double_click=True)
        state = await m.runtime.session.browser.get_state()
        field = next(el for el in state.elements if el.tag == "input" and el.value == "Walk dog")
        assert field.css_path
        from argus.resolver import describe

        typed = await _fn("type_into")(describe(field), "Walk dogs")
        assert "failed" not in typed
        pressed = await _fn("press_key")("Enter", describe(field))
        assert "failed" not in str(pressed)
        state = await m.runtime.session.browser.get_state()
        assert any(el.value == "Walk dogs" for el in state.elements)
    finally:
        await _end()
