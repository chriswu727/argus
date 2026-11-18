"""Properties of the verdict rules, checked against expectations written here.

The parity suite proves the three checkers agree; these tests state what the
answer must be without calling the code under test to find out, so a mistake
in the shared rules cannot pass by being shared.
"""

from __future__ import annotations

import random
from urllib.parse import quote

import pytest

import argus.mcp_server as m
from argus.application.text import _search_page
from argus.domain import verdict as v
from argus.domain.verification import receipt_outcome
from tests.test_verdict_parity import (
    _FRAMED,
    TARGET,
    _fn,
    _Pages,
    _serve,
    _start,
    _virtual_list,
)

NAMES = ("list", "page", "q", "sort", "tab", "id")
VALUES = ("work", "home", "2", "", "a b", "é", "x=y")
TRACKING = ("utm_source", "utm_campaign", "fbclid", "gclid")


def _url(pairs, path="/tasks"):
    query = "&".join(f"{quote(name)}={quote(value)}" for name, value in pairs)
    return "http://app.test" + path + ("?" + query if query else "")


def _selects_the_same_content(requested, landed):
    """Reference rule over (name, value) pairs: every requested value of every
    name is still there, as many times as it was asked for."""
    for name in {name for name, _ in requested}:
        asked = sorted(value for other, value in requested if other == name)
        got = sorted(value for other, value in landed if other == name)
        for value in set(asked):
            if got.count(value) < asked.count(value):
                return False
    return True


def test_moved_from_matches_the_reference_rule_on_generated_queries():
    rng = random.Random(20261117)
    seen = {True: 0, False: 0}
    for _ in range(3000):
        requested = [(rng.choice(NAMES), rng.choice(VALUES)) for _ in range(rng.randint(0, 4))]
        landed = list(requested)
        for _ in range(rng.randint(0, 3)):
            change = rng.choice(("drop", "edit", "add", "shuffle", "track"))
            if change == "drop" and landed:
                landed.pop(rng.randrange(len(landed)))
            elif change == "edit" and landed:
                at = rng.randrange(len(landed))
                landed[at] = (landed[at][0], rng.choice(VALUES))
            elif change == "add":
                landed.append((rng.choice(NAMES), rng.choice(VALUES)))
            elif change == "shuffle":
                rng.shuffle(landed)
            else:
                landed.insert(0, (rng.choice(TRACKING), "mail"))
        tracked = requested + [(rng.choice(TRACKING), "ad")] * rng.randint(0, 1)
        expected = not _selects_the_same_content(requested, landed)
        assert v.moved_from(_url(tracked), _url(landed)) is expected, (tracked, landed)
        seen[expected] += 1
    assert min(seen.values()) > 300, seen


def test_a_moved_load_never_confirms_absence():
    rng = random.Random(7)
    for _ in range(500):
        requested = [(rng.choice(NAMES), rng.choice(VALUES)) for _ in range(rng.randint(1, 3))]
        landed = list(requested)
        at = rng.randrange(len(landed))
        landed[at] = (landed[at][0], landed[at][1] + "-other")
        load = {
            "status": 200,
            "requested": _url(requested),
            "landed": _url(landed),
            "frames": [{"text": "Tasks"}],
        }
        assert v.text_observation(load, False) == (None, "moved")
        assert v.persistence_check("absent", False, load)["matches"] is None
        assert v.text_observation(load, True) == (True, None)


def test_path_host_and_hash_route_still_decide():
    assert v.moved_from("http://a.test/tasks?list=work", "http://a.test/home?list=work")
    assert v.moved_from("http://a.test/tasks?list=work", "http://b.test/tasks?list=work")
    assert v.moved_from("http://a.test/#/tasks?list=work", "http://a.test/#/tasks?list=home")
    assert not v.moved_from("http://a.test/tasks?list=work#top", "http://a.test/tasks/?list=work")
    assert not v.moved_from("http://a.test/tasks?a=1&b=2", "http://a.test/tasks?b=2&a=1")
    assert not v.moved_from("http://a.test/tasks?q=a+b", "http://a.test/tasks?q=a%20b")


@pytest.fixture(scope="module")
def site():
    class Site(_Pages):
        pages: dict = {}

    server, base = _serve(Site)
    try:
        yield Site, base
    finally:
        server.shutdown()
        server.server_close()


async def test_a_row_that_exists_is_never_reported_absent(site):
    """Generated virtual lists, scrolled by the window, by a box of any height
    or inside an iframe: wherever the row is, the search finds it or says it
    did not finish. A row within easy reach of the scan must be found."""
    handler, base = site
    rng = random.Random(45)
    # (rows, height of the scrolling box or None for the window, inside an iframe)
    layouts = [
        (
            rng.randint(30, 900),
            rng.choice([None, None, 400, rng.randint(8, 700)]),
            rng.random() < 0.4,
        )
        for _ in range(16)
    ]
    layouts += [(150, None, False), (150, 400, False), (900, None, False), (900, 400, True)]
    layouts += [(40, None, True), (20, 40, False), (12, 24, False), (30, 9, True)]
    await _start(base + "/blank")
    found = unfinished = 0
    try:
        session = m.runtime.session
        for index, (rows, box, framed) in enumerate(layouts):
            target_row = rng.randrange(rows)
            listing = _virtual_list(rows, target_row, box is not None, box or 0)
            handler.pages = {f"/list{index}": (200, listing, None)}
            if framed:
                handler.pages[f"/page{index}"] = (200, _FRAMED.format(src=f"/list{index}"), None)
            await _fn("navigate")(f"{base}/{'page' if framed else 'list'}{index}")
            state = await session.browser.get_state()
            present, searched = await _search_page(session, TARGET, state)
            layout = (rows, target_row, box, framed)
            assert present or not searched, f"reported absent: {layout}"
            # What scrolls shows this much at a time: the box, the 400px frame
            # or the 720px page. Twenty steps of four fifths of it is well
            # inside the scan's budget, so a row that near must be found.
            visible = box or (400 if framed else 720)
            if target_row * 40 <= 20 * 0.8 * visible:
                assert present, f"not found within easy reach: {layout}"
            found += present
            unfinished += not present
    finally:
        await _fn("end_session")()
    assert found >= 8 and unfinished >= 2, (found, unfinished)


async def test_a_search_that_stops_early_is_inconclusive_in_both_live_checks(site):
    handler, base = site
    handler.pages = {"/feed": (200, _virtual_list(4000, 3990), None)}
    url = base + "/feed"
    await _start(url)
    try:
        await _fn("record_bug")(
            "row lost",
            "high",
            {"expected": "x", "actual": "y"},
            verify={"expect": "absent", "target_text": TARGET, "at_url": url},
        )
        receipt = m.runtime.session.bugs[-1].reproduction_receipt
        assert receipt["reproduced"] is None and "not scrolled to its end" in receipt["reason"]
        assert receipt_outcome(receipt) is None
        result = await _fn("verify_persistence")("absent", TARGET, url)
        assert result.data["verification"]["matches"] is None
        assert "INCONCLUSIVE" in result and "not scrolled to its end" in result
    finally:
        await _fn("end_session")()
