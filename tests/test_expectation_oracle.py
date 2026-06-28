"""Predict-then-check expectation oracle (test_action's `expect`).

Forming an expectation and catching the SURPRISE is the senior-tester move that
ordinary FE/BE tests can't make (they only assert what the author already knew).
Pure evaluator tests + one live test_action wiring smoke.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import argus.mcp_server as m
from argus.mcp_server import _evaluate_expectation
from tests.conftest import make_page_state, make_element


def _all_ok(results):
    return bool(results) and all(ok for _, ok, _ in results)


def test_count_delta_match_and_fake_add_surprise():
    before = make_page_state(counts={"tasks": 3})
    assert _all_ok(_evaluate_expectation(
        before, make_page_state(counts={"tasks": 4}), {"count": {"label": "tasks", "delta": 1}}))
    # fake add: toast says added but the count never moved -> SURPRISE
    assert not _all_ok(_evaluate_expectation(
        before, make_page_state(counts={"tasks": 3}), {"count": {"label": "tasks", "delta": 1}}))


def test_list_gains_and_fake_delete_surprise():
    before = make_page_state(item_lists={"t": ["Buy milk - high - Edit Delete"]})
    after = make_page_state(item_lists={"t": ["Buy milk - high - Edit Delete",
                                              "Walk dog - low - Edit Delete"]})
    assert _all_ok(_evaluate_expectation(before, after, {"gains": "Walk dog"}))
    # fake delete: expected to remove 'Buy milk' but it's still listed -> SURPRISE
    assert not _all_ok(_evaluate_expectation(before, after, {"removes": "Buy milk"}))


def test_text_toast_url_predicates():
    before = make_page_state(page_text="Dashboard", url="https://x/a")
    after = make_page_state(page_text="Saved! Dashboard", toast_messages=["Saved!"], url="https://x/b")
    assert _all_ok(_evaluate_expectation(
        before, after, {"text_present": "Saved", "toast": "Saved", "url_changed": True}))
    assert not _all_ok(_evaluate_expectation(before, after, {"text_absent": "Saved"}))


def test_in_place_edit_is_not_a_fake_add_or_delete():
    # Priority low->high: same item, different full-row text. Must NOT read as a
    # gain or a remove (F1 — the false MATCH that would confirm a fake delete).
    before = make_page_state(item_lists={"t": ["Buy milk - low - Edit Delete"]})
    after = make_page_state(item_lists={"t": ["Buy milk - high - Edit Delete"]})
    assert _evaluate_expectation(before, after, {"gains": "Buy milk"})[0][1] is False
    assert _evaluate_expectation(before, after, {"removes": "Buy milk"})[0][1] is False


def test_text_checks_ignore_input_values():
    # A deleted item lingering in an edit-form input value must read as ABSENT
    # (F2 — text checks use visible text only, never el.value).
    el = make_element(tag="input", value="Buy milk")
    before = make_page_state(item_lists={"t": ["Buy milk row"]})
    after = make_page_state(elements=[el], item_lists={"t": []})
    assert _evaluate_expectation(before, after, {"text_absent": "Buy milk"})[0][1] is True


def test_count_label_not_found_is_unchecked_not_surprise():
    # Unmeasurable (label absent) must be UNCHECKED (None), not a false SURPRISE (F3).
    res = _evaluate_expectation(make_page_state(counts={}), make_page_state(counts={}),
                               {"count": {"label": "tasks", "delta": 1}})
    assert res[0][1] is None


def test_unknown_key_is_surfaced_as_unchecked():
    res = _evaluate_expectation(make_page_state(), make_page_state(), {"gainz": "x"})
    assert any(ok is None for _, ok, _ in res)  # F5 — typo not silently dropped


def test_type_coercion_for_delta_and_url_changed():
    before = make_page_state(counts={"t": 2}, url="https://x/a")
    after = make_page_state(counts={"t": 3}, url="https://x/a")
    # stringified delta still compares numerically (F6)
    assert _evaluate_expectation(before, after, {"count": {"label": "t", "delta": "1"}})[0][1] is True
    # "false" parsed as boolean False -> matches the no-change reality
    assert _evaluate_expectation(before, after, {"url_changed": "false"})[0][1] is True


def test_text_present_requires_appearance_not_preexistence():
    # Pre-existing 'Saved' did not appear because of the action -> SURPRISE (F7).
    before = make_page_state(page_text="Saved drafts")
    after = make_page_state(page_text="Saved drafts")
    assert _evaluate_expectation(before, after, {"text_present": "Saved"})[0][1] is False


_PAGE = ("<html><body><button id=s>Save</button><div id=m></div>"
         "<script>document.getElementById('s').onclick=()=>"
         "document.getElementById('m').textContent='Saved!';</script></body></html>")


async def _start_on(html):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    fn = m.start_session.fn if hasattr(m.start_session, "fn") else m.start_session
    try:
        await fn(Path(f.name).as_uri())
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


async def test_action_reports_match_and_surprise_live():
    await _start_on(_PAGE)
    ta = m.test_action.fn if hasattr(m.test_action, "fn") else m.test_action
    end = m.end_session.fn if hasattr(m.end_session, "fn") else m.end_session
    try:
        await m._session.browser.get_state()  # populate _last_elements
        from argus.mcp_server import observe
        await (observe.fn if hasattr(observe, "fn") else observe)()
        out = await ta("Save", expect={"text_present": "Saved"})
        assert "[MATCH" in out and "All predictions held" in out
        out2 = await ta("Save", expect={"text_present": "Nope-not-here"})
        assert "SURPRISE" in out2 and "SURPRISE(S)" in out2
    finally:
        await end()
