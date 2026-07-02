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


def _row(idx, title, label, tag="button"):
    """One control inside a task card. parent_context is the WHOLE card's
    text — including sibling controls — exactly as the browser extracts it.
    This is what made naive word-set matching collide across a row."""
    el = make_element(idx, tag=tag, text=label)
    el.parent_context = f"{title} high 1.0 days ago Edit Delete"
    return el


def test_row_scoped_match_picks_the_right_rows_control():
    # Two task cards, each with Edit + Delete. "Delete Buy groceries" must
    # land on the Delete in the Buy-groceries row — not its row-mate Edit
    # (whose card text also contains the word "Delete"), nor the Delete in
    # the other row (whose card lacks "Buy groceries").
    els = [
        _row(0, "Buy groceries", "Edit"),
        _row(1, "Buy groceries", "Delete"),
        _row(2, "Fix login page CSS", "Edit"),
        _row(3, "Fix login page CSS", "Delete"),
    ]
    r = resolve_element("Delete Buy groceries", els)
    assert r.reason == "unique"
    assert r.found is els[1]
    # Natural tester scaffolding ("in the ... row") must not break the match.
    r2 = resolve_element("Delete in the Buy groceries row", els)
    assert r2.reason == "unique"
    assert r2.found is els[1]


def test_ordinal_selects_nth_identical_control():
    els = [make_element(i, tag="button", text="Delete") for i in range(5)]
    r = resolve_element("Delete #2", els)
    assert r.reason == "unique"
    assert r.found is els[1]  # 1-based: #2 -> index 1
    r2 = resolve_element("the 3rd Delete", els)
    assert r2.reason == "unique"
    assert r2.found is els[2]


def test_quoted_labels_resolve():
    # Agents habitually quote the label: `link "Tasks"`, `input "you@x"`.
    els = [make_element(0, tag="a", text="Tasks"), make_element(1, tag="a", text="Home")]
    r = resolve_element('link "Tasks"', els)
    assert r.reason == "unique" and r.found is els[0]
    els2 = [make_element(0, tag="input", type="email", placeholder="you@example.com")]
    r2 = resolve_element('input "you@example.com"', els2)
    assert r2.reason == "unique" and r2.found is els2[0]


def test_next_to_is_row_scoping_like_near():
    # "next to X" == "near X" scaffolding; a standalone "Next" button still resolves.
    rows = [_row(0, "Buy groceries", "Edit"), _row(1, "Buy groceries", "Delete"),
            _row(2, "Pay rent", "Edit"), _row(3, "Pay rent", "Delete")]
    r = resolve_element('Delete next to "Buy groceries"', rows)
    assert r.reason == "unique" and r.found is rows[1]
    nxt = [make_element(0, tag="button", text="Next"), make_element(1, tag="button", text="Back")]
    assert resolve_element("Next button", nxt).found is nxt[0]


def test_parenthetical_context_is_stripped():
    def mk(i, name, value):
        e = make_element(i, tag="input", type="text", name=name); e.value = value; return e
    els = [mk(0, "title", "Buy groceries"), mk(1, "priority", "high")]
    assert resolve_element("Title field (Buy groceries)", els).found is els[0]
    assert resolve_element("Priority field (high)", els).found is els[1]


def test_double_kind_word_does_not_pollute_core():
    els = [make_element(0, tag="input", type="text", name="cvv"),
           make_element(1, tag="input", type="text", name="card")]
    # "input field" is two kind words — "field" must not leak into the core and
    # break the strict type_into path (which cannot fall back).
    assert resolve_element("card input field", els, kind_filter="input", strict_kind=True).found is els[1]
    assert resolve_element("the card field input", els).found is els[1]  # two kind words + a stopword


def test_snake_case_name_matches_natural_language():
    els = [make_element(0, tag="input", type="text", name="first_name"),
           make_element(1, tag="input", type="text", name="last_name"),
           make_element(2, tag="input", type="number", name="qty_1", value="2")]
    assert resolve_element("first name field", els).found is els[0]
    assert resolve_element("last name input", els).found is els[1]
    assert resolve_element("qty field", els).found is els[2]  # underscore no longer joins tokens


def test_input_matched_by_current_value():
    # a pre-filled field (e.g. display name showing "Alex") is targetable by value
    els = [make_element(0, tag="input", type="text", value="Alex"),
           make_element(1, tag="button", text="Save")]
    assert resolve_element("input with Alex", els).found is els[0]  # partial/core via value
    assert resolve_element("Alex", els).found is els[0]             # exact value (fast path)


def test_wrong_kind_hint_falls_back_when_pool_empty():
    # agent calls a link a "button" while real buttons fill the pool -> still found
    els = [make_element(0, tag="a", text="+ New Task"), make_element(1, tag="button", text="Save"),
           make_element(2, tag="button", text="Cancel")]
    assert resolve_element("+ New Task button", els).found is els[0]
    # but a hint that DOES match is still respected (no spurious fallback)
    e2 = [make_element(0, tag="a", text="email"),
          make_element(1, tag="input", type="email", name="email", placeholder="you@x")]
    assert resolve_element("email field", e2).found is e2[1]


def test_link_resolves_by_href_and_to_is_scaffolding():
    def mk(i, text, href):
        e = make_element(i, tag="a", text=text); e.href = href; return e
    els = [mk(0, "Wireless Headphones", "/product/1"), mk(1, "USB Cable", "/product/2")]
    assert resolve_element("Wireless Headphones product link", els).found is els[0]  # 'product' lives in href
    assert resolve_element("link to /product/1", els).found is els[0]                # target a link by its path
    # "to" as scaffolding must not break a literal "Add to Cart" label (fast path)
    cart = [make_element(0, tag="button", text="Add to Cart"), make_element(1, tag="button", text="Remove")]
    assert resolve_element("Add to Cart", cart).found is cart[0]


def test_kind_only_with_ordinal_picks_nth():
    els = [make_element(i, tag="input", type="checkbox") for i in range(5)]
    assert resolve_element("first checkbox", els).found is els[0]
    assert resolve_element("2nd checkbox", els).found is els[1]
    assert resolve_element("last checkbox", els).found is els[4]
    assert resolve_element("checkbox", els).reason == "ambiguous"  # 5 present, no ordinal -> honest
    # a lone checkbox still resolves from the bare kind
    one = [make_element(0, tag="input", type="checkbox"), make_element(1, tag="button", text="X")]
    assert resolve_element("checkbox", one).found is one[0]


def test_region_scaffolding_is_ignored():
    # "Register link in the navigation" -> the region words are scaffolding.
    els = [make_element(0, tag="a", text="Register"), make_element(1, tag="a", text="Home")]
    r = resolve_element("Register link in the navigation", els)
    assert r.reason == "unique" and r.found is els[0]
    # a literal "Navigation" label still resolves (exact-label fast path)
    nav = [make_element(0, tag="a", text="Navigation"), make_element(1, tag="a", text="Home")]
    assert resolve_element("Navigation", nav).found is nav[0]


def test_last_positional_and_last_name_label():
    els = [make_element(i, tag="button", text="Delete") for i in range(4)]
    r = resolve_element("last Delete button", els)
    assert r.reason == "unique" and r.found is els[3]  # last of 4
    # "Last name" must resolve as a LABEL (exact-label fast path), not positional
    form = [make_element(0, tag="input", placeholder="First name"),
            make_element(1, tag="input", placeholder="Last name")]
    r2 = resolve_element("Last name field", form)
    assert r2.reason == "unique" and r2.found is form[1]


def test_short_verb_does_not_bleed_into_longer_word():
    # F5: 'Add' must not resolve to 'Address line 1', 'Edit' not to 'Credit...'.
    # With no real control by that name the answer is no_match, never a
    # confident wrong pick.
    els = [
        make_element(0, tag="input", placeholder="Address line 1"),
        make_element(1, tag="button", text="Save changes"),
    ]
    r = resolve_element("Add", els)
    assert r.reason == "no_match" and r.found is None

    els2 = [make_element(0, tag="input", placeholder="Credit card number")]
    assert resolve_element("Edit", els2).reason == "no_match"


def test_whole_word_substring_still_matches():
    els = [make_element(0, tag="input", placeholder="Search tasks")]
    r = resolve_element("search", els)
    assert r.reason == "unique" and r.found is els[0]


def test_sign_in_resolves_despite_in_stopword():
    # F7: 'in' is a stopword; 'Sign in' must still beat 'Sign up'.
    els = [make_element(0, tag="button", text="Sign in"),
           make_element(1, tag="button", text="Sign up")]
    assert resolve_element("Sign in", els).found is els[0]
    assert resolve_element("Sign in button", els).found is els[0]

    els2 = [make_element(0, tag="a", text="Log in"),
            make_element(1, tag="a", text="Log out")]
    assert resolve_element("Log in", els2).found is els2[0]


def test_literal_hash_label_not_hijacked_as_ordinal():
    # F4: 'Issue #42' is a real label, not "the 42nd Issue".
    els = [make_element(0, tag="a", text="Issue #1"),
           make_element(1, tag="a", text="Issue #2"),
           make_element(2, tag="a", text="Issue #42")]
    r = resolve_element("Issue #42", els)
    assert r.reason == "unique" and r.found is els[2]

    # ...even when the matching label is not in positional order (the old code
    # silently returned band[0]).
    els2 = [make_element(0, tag="a", text="Issue #42"),
            make_element(1, tag="a", text="Issue #2"),
            make_element(2, tag="a", text="Issue #1")]
    r2 = resolve_element("Issue #1", els2)
    assert r2.reason == "unique" and r2.found is els2[2]


def test_ordinal_out_of_range_is_ambiguous():
    els = [make_element(i, tag="button", text="Delete") for i in range(3)]
    r = resolve_element("Delete #9", els)
    assert r.reason == "ambiguous"
    assert r.found is None


def test_lone_ordinal_word_stays_a_label():
    # "second" alone is the label, not a position selector.
    els = [make_element(0, tag="button", text="Second"), make_element(1, tag="button", text="First")]
    r = resolve_element("Second", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_extract_ordinal_forms():
    from argus.resolver import extract_ordinal
    assert extract_ordinal("Delete #2") == ("Delete", 2)
    assert extract_ordinal("the 3rd Edit") == ("the  Edit", 3)
    assert extract_ordinal("second Delete button") == ("Delete button", 2)
    assert extract_ordinal("Delete") == ("Delete", None)


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


def test_screen_ordinal_selects_nth_identical_control():
    # Parity with the web resolver (F13): ordinal disambiguation in screen mode,
    # scanned in reading order (top-to-bottom).
    from argus.resolver import resolve_screen_element
    els = [_screen_element("AXButton", title="Delete", y=10 * i) for i in range(4)]
    r = resolve_screen_element("Delete #2", els)
    assert r.reason == "unique"
    assert r.found is els[1]


def test_screen_exact_label_survives_stopword():
    # "Sign in" must not collapse to "sign" and tie with "Sign up" (F13 parity).
    from argus.resolver import resolve_screen_element
    els = [_screen_element("AXButton", title="Sign in"),
           _screen_element("AXButton", title="Sign up")]
    r = resolve_screen_element("Sign in", els)
    assert r.reason == "unique"
    assert r.found is els[0]


def test_describe_screen_includes_role_and_coords():
    from argus.resolver import describe_screen
    el = _screen_element("AXButton", title="Save", x=120, y=80, w=80, h=30,
                         path=["Settings", "General"])
    out = describe_screen(el)
    assert "AXButton" in out
    assert "Save" in out
    assert "120" in out and "80" in out
    assert "Settings" in out or "General" in out
