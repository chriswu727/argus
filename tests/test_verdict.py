"""The shared verdict rules, on plain data."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from argus.domain import verdict as v

SOURCE = Path(v.__file__).read_text(encoding="utf-8")


def frame(text="", **facts):
    return {
        "text": text,
        "fields": [],
        "headings": [],
        "buttons": [],
        "password": False,
        "truncated": False,
        **facts,
    }


def load(*frames, **facts):
    return {
        "status": 200,
        "requested": "http://a.test/tasks",
        "landed": "http://a.test/tasks",
        "frames": list(frames) or [frame()],
        "settled": True,
        "login_page": False,
        "login_page_recorded": False,
        **facts,
    }


def test_module_is_standalone_and_emittable():
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module in {"urllib.parse"}, node.module
            assert node.module != "__future__"
        if isinstance(node, ast.Import):
            assert all(alias.name in {"re"} for alias in node.names)
    assert isinstance(v.VERDICT_VERSION, str) and v.VERDICT_VERSION


@pytest.mark.parametrize(
    "needle,haystack,expected",
    [
        ("category", "Browse by category here", True),
        ("cat", "Browse by category here", False),
        ("delete", "Recently Deleted: none", False),
        ("buy groceries", "  buy   groceries  ", True),
        ("已完成", "任务已完成", True),
        ("-50%", "$89.99-50%", True),
        ("", "anything", False),
    ],
)
def test_token_present(needle, haystack, expected):
    assert v.token_present(needle, haystack) is expected


def test_presence_reads_visible_text_of_any_frame_and_field_values():
    frames = [frame("Tasks"), frame("Buy groceries")]
    assert v.frames_present("Buy groceries", frames)
    assert v.frames_present("Draft", [frame("Tasks", fields=["Draft"])])
    assert not v.frames_present("Draft", [frame("Tasks", headings=["Draft"], buttons=["Draft"])])


def test_login_phrases_count_only_near_the_top_of_the_main_frame():
    assert v.looks_logged_out(frame("Please log in to continue"))
    assert not v.looks_logged_out(frame("x" * 5000 + " Log in to reply"))
    assert v.looks_logged_out(frame(headings=["Sign in"], password=True))
    assert not v.looks_logged_out(frame(headings=["Sign in"]))
    assert v.is_login_page(frame(headings=["Sign in"], password=True))
    assert not v.is_login_page(frame("Session expired", headings=["Sign in"], password=True))


def test_login_form_recognises_other_languages():
    assert v.login_form(frame(password=True, buttons=["Anmelden"]))
    assert v.login_form(frame(password=True, headings=["ログイン"]))
    assert not v.login_form(frame(buttons=["Anmelden"]))
    assert not v.login_form(frame(password=True, buttons=["Save"]))


def test_moved_from_compares_pages_not_anchors():
    assert v.moved_from("http://a.test/cart", "http://a.test/login?next=/cart")
    assert v.moved_from("http://a.test/#/tasks", "http://a.test/#/login")
    assert not v.moved_from("http://a.test/cart", "http://a.test/cart/")
    assert not v.moved_from("http://a.test/", "http://a.test/#section")
    assert not v.moved_from("http://a.test/x", "")


@pytest.mark.parametrize(
    "facts,present,expected",
    [
        ({}, True, (True, None)),
        ({}, False, (False, None)),
        ({"status": 500}, True, (None, "http_error")),
        ({"status": 404}, False, (None, "http_error")),
        ({"status": None}, False, (False, None)),
        ({"frames": [frame("Please sign in to continue")]}, True, (None, "login_wall")),
        ({"landed": "http://a.test/home"}, True, (True, None)),
        ({"landed": "http://a.test/home"}, False, (None, "moved")),
        ({"frames": [frame(truncated=True)]}, False, (None, "truncated")),
        ({"frames": [frame(), frame(truncated=True)]}, False, (None, "truncated")),
        ({"settled": False}, True, (True, None)),
        ({"settled": False}, False, (None, "unsettled")),
        ({"frames": [frame(password=True, buttons=["Anmelden"])]}, False, (None, "login_form")),
        ({"frames": [frame(password=True, buttons=["Anmelden"])]}, True, (True, None)),
    ],
)
def test_text_observation(facts, present, expected):
    assert v.text_observation(load(**facts), present) == expected


def test_the_recorded_login_page_may_be_checked_itself():
    sign_in = frame(headings=["Sign in"], password=True)
    assert v.load_gate(load(sign_in)) == "login_wall"
    assert v.load_gate(load(sign_in, login_page=True)) is None
    assert v.load_gate(load(sign_in, login_page=True, landed="http://a.test/x")) == "login_wall"
    assert v.text_observation(load(sign_in, login_page=True), False) == (None, "login_form")
    assert v.text_observation(load(sign_in, login_page=True, login_page_recorded=True), False) == (
        False,
        None,
    )


@pytest.mark.parametrize(
    "expect,present,status,expect_status,conclusive,matches",
    [
        ("absent", False, 404, 0, False, None),
        ("absent", False, 404, 404, True, True),
        ("absent", True, 200, 404, True, False),
        ("present", True, 500, 0, True, True),
        ("absent", True, 500, 0, True, False),
        ("absent", False, None, 404, False, None),
        ("absent", False, 200, 0, True, True),
    ],
)
def test_persistence_policies(expect, present, status, expect_status, conclusive, matches):
    result = v.persistence_check(expect, present, load(status=status), expect_status)
    assert result["conclusive"] is conclusive and result["matches"] is matches


def test_status_observation():
    assert v.status_observation(404, 404, 404, False, False) == (True, None)
    assert v.status_observation(404, 200, 200, False, False) == (False, None)
    assert v.status_observation(404, 500, 500, False, False)[0] is None
    assert v.status_observation(200, 200, 200, False, True)[0] is None
    assert v.status_observation(200, None, None, False, False)[0] is None


def test_receipt_verdict_and_outcome():
    assert v.receipt_verdict([True, True], "present")["reproduced"] is True
    flaky = v.receipt_verdict([True, False], "present")
    assert flaky["flaky"] is True and v.receipt_outcome(flaky) is None
    assert v.receipt_outcome(v.receipt_verdict([True, True], "absent")) is False
    assert v.receipt_outcome(None) is None


def test_scroll_budget():
    assert v.scroll_exhausted(v.MAX_SCROLLS, False, 10, 0)
    assert v.scroll_exhausted(1, True, 10, 0)
    assert v.scroll_exhausted(1, False, 10, 10)
    assert not v.scroll_exhausted(1, False, 10, 0)


def test_every_reason_has_text():
    for code in v.REASON_CODES:
        assert v.reason_text(code) != code
