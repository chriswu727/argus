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
