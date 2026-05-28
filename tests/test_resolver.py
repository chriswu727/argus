"""Tests for argus.resolver — natural-language element resolution."""
from __future__ import annotations

from argus.resolver import resolve_element, split_description, kind_of, describe
from .conftest import make_element


def test_no_elements_returns_no_elements():
    r = resolve_element("anything", [])
    assert r.reason == "no_elements"
    assert r.found is None


def test_exact_text_match_wins():
    els = [
        make_element(0, tag="a", text="Home"),
        make_element(1, tag="a", text="Tasks"),
        make_element(2, tag="button", text="Add Task"),
    ]
    r = resolve_element("Add Task", els)
    assert r.reason == "unique"
    assert r.found is els[2]


def test_kind_hint_strips_and_filters():
    els = [
        make_element(0, tag="a", text="Submit"),         # link with text "Submit"
        make_element(1, tag="button", text="Submit"),     # button — should win
    ]
    r = resolve_element("Submit button", els)
    assert r.reason == "unique"
    assert r.found is els[1]


def test_field_hint_resolves_input():
    els = [
        make_element(0, tag="a", text="email"),                                  # link labeled "email"
        make_element(1, tag="input", type="email", placeholder="you@example.com",
                     name="email"),                                              # actual input
    ]
    r = resolve_element("email field", els)
    assert r.reason == "unique"
    assert r.found is els[1]


def test_ambiguous_returns_top_candidates():
    els = [
        make_element(0, tag="button", text="Submit"),
        make_element(1, tag="button", text="Submit"),
        make_element(2, tag="button", text="Submit"),
    ]
    r = resolve_element("Submit", els)
    assert r.reason == "ambiguous"
    assert r.found is None
    assert len(r.candidates) >= 2


def test_no_match_returns_no_match():
    els = [make_element(0, tag="a", text="Home"), make_element(1, tag="a", text="Tasks")]
    r = resolve_element("Subscribe", els)
    assert r.reason == "no_match"
    assert r.found is None


def test_placeholder_substring_match():
    els = [
        make_element(0, tag="input", type="text", placeholder="Search tasks..."),
        make_element(1, tag="input", type="text", placeholder="Your email"),
    ]
    r = resolve_element("search", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_aria_label_match():
    els = [
        make_element(0, tag="button", text="X"),  # decorative-ish
    ]
    els[0].aria_label = "Close dialog"
    r = resolve_element("Close dialog", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_word_set_match_for_loose_phrasing():
    els = [
        make_element(0, tag="button", text="Save Settings"),
        make_element(1, tag="button", text="Cancel"),
    ]
    # "settings save" — words present but not in order
    r = resolve_element("settings save", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_stopwords_dont_block_match():
    els = [make_element(0, tag="button", text="Login")]
    r = resolve_element("the login button", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_visible_match_beats_id_attribute_leak():
    # els[1]'s id contains "email" but nothing a user sees does. The real
    # field exposes "email" in its placeholder. A visible/placeholder hit
    # must win decisively — an internal id substring must not drag the
    # result into ambiguity (regression for the W-09/W-19 attr leak).
    els = [
        make_element(0, tag="input", type="text", placeholder="Enter your email"),
        make_element(1, tag="input", type="text", id="email-confirm-wrapper"),
    ]
    r = resolve_element("email", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_kind_of_categorises_correctly():
    assert kind_of(make_element(0, tag="a")) == "link"
    assert kind_of(make_element(0, tag="button")) == "button"
    assert kind_of(make_element(0, tag="input", type="email")) == "input"
    assert kind_of(make_element(0, tag="input", type="checkbox")) == "checkbox"
    assert kind_of(make_element(0, tag="input", type="submit")) == "button"
    assert kind_of(make_element(0, tag="select")) == "select"


def test_split_description_strips_kind_hint():
    assert split_description("Login button") == ("login", "button")
    assert split_description("the email field") == ("email", "input")
    assert split_description("Submit") == ("submit", None)


def test_describe_renders_useful_label():
    el = make_element(0, tag="button", text="Delete")
    el.parent_context = "Buy groceries — high — Edit Delete"
    text = describe(el)
    assert "button" in text
    assert "Delete" in text
    assert "near:" in text


# ── Screen-mode resolver tests ───────────────────────────────────────


def _screen_element(role, title="", value="", description="", x=0, y=0, w=10, h=10, path=None):
    """Lightweight ScreenElement-shaped object for tests (no PyObjC needed)."""
    class _SE:
        pass
    se = _SE()
    se.role = role
    se.role_description = role
    se.title = title
    se.value = value
    se.description = description
    se.enabled = True
    se.focused = False
    se.x = x
    se.y = y
    se.width = w
    se.height = h
    se.path = path or []
    se._ax_ref = None
    return se


def test_screen_resolve_button_via_kind_hint():
    from argus.resolver import resolve_screen_element
    elements = [
        _screen_element("AXStaticText", title="Save"),
        _screen_element("AXButton", title="Save"),
    ]
    r = resolve_screen_element("Save button", elements)
    assert r.reason == "unique"
    assert r.found.role == "AXButton"


def test_screen_resolve_text_field_via_field_hint():
    from argus.resolver import resolve_screen_element
    elements = [
        _screen_element("AXButton", title="email"),
        _screen_element("AXTextField", description="Email", value=""),
    ]
    r = resolve_screen_element("email field", elements)
    assert r.reason == "unique"
    assert r.found.role == "AXTextField"


def test_screen_resolve_uses_path_for_disambiguation():
    from argus.resolver import resolve_screen_element
    elements = [
        _screen_element("AXButton", title="Submit", path=["Login dialog"]),
        _screen_element("AXButton", title="Submit", path=["Comment box"]),
    ]
    # No qualifier: ambiguous.
    r = resolve_screen_element("Submit", elements)
    assert r.reason == "ambiguous"
    # With path word: unique.
    r = resolve_screen_element("Submit Login", elements)
    assert r.reason == "unique"
    assert r.found.path == ["Login dialog"]


def test_describe_screen_includes_role_and_coords():
    from argus.resolver import describe_screen
    el = _screen_element("AXButton", title="Save", x=120, y=80, w=80, h=30,
                         path=["Settings", "General"])
    out = describe_screen(el)
    assert "AXButton" in out
    assert "Save" in out
    assert "120" in out and "80" in out
    assert "Settings" in out or "General" in out
