"""Final hardening: report URL-secret redaction, journal only-reproduced filter,
and the login-wall heuristic (guards the clean-load receipt from certifying off
an error/logged-out page)."""
from __future__ import annotations

import argus.mcp_server as m
from argus.models import Bug, BugType, ExplorationResult, Severity
from argus.reporter import Reporter
from tests.conftest import make_page_state


def _bug(url="http://x/", receipt=None):
    return Bug(type=BugType.UX_ISSUE, severity=Severity.HIGH, title="t", description="d",
               url=url, steps_to_reproduce=[], reproduction_receipt=receipt)


def test_report_redacts_url_secrets():
    bug = _bug(url="http://x/reset?token=eyJabc.def.ghi&u=alice")
    bug.network_logs = [{"method": "POST", "url": "http://x/api?access_token=eyJxx.yy.zz", "status": 200}]
    html = Reporter()._build_html(ExplorationResult(
        url="http://x", bugs=[bug], pages_visited=[], actions_taken=0,
        duration_seconds=0.0, focus_areas=[]))
    assert "eyJabc.def.ghi" not in html
    assert "eyJxx.yy.zz" not in html
    assert "redacted" in html.lower()
    assert "alice" in html  # non-secret query param survives


def test_report_auto_captured_hides_misleading_journey_steps():
    auto = _bug(receipt={"attempted": False, "auto_captured": True})
    auto.title = "Console exception: appConfig is not defined"
    auto.steps_to_reproduce = ["click_what('Login button')", "type_into('email')", "navigate('/')"]
    manual = _bug(receipt={"attempted": True, "reproduced": True})
    manual.title, manual.steps_to_reproduce = "Real journey bug", ["click Delete", "verify still-there"]
    html = Reporter()._build_html(ExplorationResult(
        url="u", bugs=[auto, manual], pages_visited=[], actions_taken=0,
        duration_seconds=0.0, focus_areas=[]))
    assert "How it surfaced" in html                 # auto-captured: honest surfacing note
    assert "Login button" not in html                # its misleading journey step is NOT shown
    assert "Steps to reproduce" in html and "verify still-there" in html  # manual keeps real steps


def test_report_leads_with_verified_and_keeps_per_bug_severity():
    from argus.reporter import Reporter, _trust_rank
    v = _bug(receipt={"attempted": True, "reproduced": True})
    v.severity, v.title = Severity.LOW, "PROVEN-FINDING"
    a = _bug(receipt={"attempted": False, "auto_captured": True})
    a.severity, a.title = Severity.HIGH, "AUTO-NOISE"
    html = Reporter()._build_html(ExplorationResult(
        url="u", bugs=[a, v], pages_visited=[], actions_taken=0,
        duration_seconds=0.0, focus_areas=[]))
    # verified finding leads despite LOWER severity; auto-captured noise sinks
    assert html.index("PROVEN-FINDING") < html.index("AUTO-NOISE")
    # each card shows its OWN severity (regression: a leaked loop var showed one)
    assert ">LOW<" in html and ">HIGH<" in html
    assert _trust_rank(v) == 0 and _trust_rank(a) == 3


def test_report_steps_trim_and_collapse():
    from argus.reporter import _format_steps
    html = _format_steps([f"setup step {i}" for i in range(20)] + ["click Load More", "click Load More"])
    assert "omitted" in html          # long setup preamble trimmed to the tail
    assert "(x2)" in html             # consecutive duplicates collapsed
    assert "omitted" not in _format_steps(["click A", "verify B"])  # short lists untouched


def test_bench_score_verified_offcatalog_is_not_fp():
    from argus.bench.agent_runner import score
    v = _bug(receipt={"attempted": True, "reproduced": True})
    v.title, v.description = "totally offbeat glitch", "weird thing"
    u = _bug(receipt=None)
    u.title, u.description = "another offbeat noise", "meh"
    # an auto-captured off-catalog console error is a real observed event, NOT an FP
    a = _bug(receipt={"attempted": False, "auto_captured": True})
    a.title, a.description = "captured glitch zzz", "some observed noise"
    s = score([v, u, a])
    assert s["unmatched"] == 3       # all three off the fuzzy catalog
    assert s["fp_candidates"] == 1   # only `u` (unverified, not auto-captured) is an FP candidate
    assert s["verified"] == 1        # the verified off-catalog find is a real bug, not an FP


def test_network_detector_handles_failed_request_no_status():
    from argus.detector import Detector
    d = Detector()
    # a resource that failed with net::ERR_* has no HTTP status — must not crash
    # (was `err["status"] >= 500`) and must still produce a finding.
    bugs = d.process_network_errors(
        [{"method": "GET", "url": "https://x/app.js", "status": None,
          "failure": "net::ERR_HTTP2_PROTOCOL_ERROR"}], "https://x/", [])
    assert len(bugs) == 1 and "failed to load" in bugs[0].title.lower()
    assert "ERR_HTTP2" in bugs[0].description
    # a normal 500 alongside still works
    b2 = d.process_network_errors([{"method": "POST", "url": "https://x/api", "status": 500}], "https://x/", [])
    assert b2 and "500" in b2[0].title


def test_new_events_line_peeks_since_counts():
    from types import SimpleNamespace as NS
    s = NS(browser=NS(console_errors=[{"text": "boom"}],
                      network_errors=[{"status": 500, "method": "POST", "url": "/api/x"}]))
    line = m._new_events_line(s, 0, 0)
    assert "500" in line and "get_errors" in line
    assert m._new_events_line(s, 1, 1) == ""  # nothing new since the given counts (peek, not drain)


def test_toast_line_surfaces_claim_and_stays_quiet_when_empty():
    line = m._toast_line(["Task created!", "Welcome"])
    assert "Task created!" in line and "CLAIM" in line and "persisted" in line
    assert m._toast_line([]) == "" and m._toast_line(None) == ""


async def test_get_state_retries_on_execution_context_destroyed():
    from argus.browser import BrowserDriver
    d = BrowserDriver(headless=True)
    calls = {"n": 0}

    class _FakePage:
        url = "http://x/after-nav"
        async def title(self):
            return "After"
        async def wait_for_load_state(self, *a, **k):
            pass

    d._page = _FakePage()

    async def _elts(page):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
        return []

    async def _content(page):
        return {}

    d._extract_elements = _elts
    d._extract_page_content = _content
    st = await d.get_state()
    assert calls["n"] == 2 and st.url == "http://x/after-nav"  # retried once past the nav race


def test_count_delta_note_same_url_only():
    from types import SimpleNamespace as NS
    st = NS(url="/tasks", counts={"Total": 8, "Pending": 6})
    note = m._count_delta_note({"Total": 7, "Pending": 6}, "/tasks", st)
    assert "Total" in note and "7 -> 8" in note and "Pending" not in note  # only changed count
    assert m._count_delta_note({"Total": 7}, "/other-page", st) == ""       # navigated -> no note
    assert m._count_delta_note(None, "/tasks", st) == ""                    # no prior -> no note


def test_repro_detail_surfaces_what_was_checked():
    from argus.reporter import _repro_detail
    ok = _repro_detail({"attempted": True, "reproduced": True, "target_text": "Buy groceries",
                        "expect": "present", "at_url": "/tasks"})
    # plain-sentence wording; URL intentionally dropped (the card shows it above)
    assert "Independently confirmed" in ok and "Buy groceries" in ok and "is present on the page" in ok
    # observation-based / auto-captured / inconclusive carry no detail line
    assert _repro_detail(None) == ""
    assert _repro_detail({"attempted": False, "auto_captured": True}) == ""
    assert _repro_detail({"attempted": True, "reproduced": None, "target_text": "x"}) == ""


def test_near_duplicate_catches_repeats_not_distinct():
    b = _bug()
    b.title, b.description = "Task creation toast lies — task not persisted", "toast says saved, gone on refresh"
    # exact (normalized) title repeat -> caught even with different body
    assert m._near_duplicate("Task creation toast lies — task not persisted", "reworded body", [b]) is b
    # different title but ~identical body -> Jaccard catches it
    b2 = _bug(); b2.title, b2.description = "X", "the task list count is off by one after adding an item"
    assert m._near_duplicate("Y", "the task list count is off by one after adding an item", [b2]) is b2
    # genuinely distinct finding sharing a word or two -> NOT merged
    assert m._near_duplicate("Navbar shows Login after auth", "header still shows login button", [b]) is None


def test_nearest_labels_suggests_by_token_overlap():
    from tests.conftest import make_element
    els = [make_element(tag="a", text="Tasks"), make_element(tag="a", text="Home"),
           make_element(tag="a", text="Settings")]
    near = m._nearest_labels("Back to Tasks link", els)
    assert near and "Tasks" in near[0]         # the Tasks link ranks first (shares 'tasks')
    assert m._nearest_labels("zzzzz nonexistent", els) == []  # no overlap -> no noise


def test_journal_only_persists_reproduced(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path))
    s = m.Session()
    s.url = "http://nope.test/"
    b = _bug(url="http://nope.test/",
             receipt={"attempted": True, "reproduced": False, "expect": "present", "target_text": "X"})
    s.bugs = [b]
    m._write_journal(s)
    assert m._journal_entries("nope.test") == []  # not-reproduced is NOT journaled

    b.reproduction_receipt["reproduced"] = True
    m._write_journal(s)
    assert len(m._journal_entries("nope.test")) == 1  # reproduced IS journaled


def test_dedup_description_drops_title_repeat():
    from argus.reporter import _dedup_description
    # LLMs constantly repeat the title verbatim as the description -> drop it
    assert _dedup_description("Login fails silently", "Login fails silently") == ""
    assert _dedup_description("Console error: appConfig is not defined", "appConfig is not defined") == ""
    # a description that genuinely elaborates is kept
    assert _dedup_description("Login fails", "Login fails because the token is never sent to the server")


def test_report_renders_string_steps_and_dedup_desc():
    # end-to-end: a bug whose model gave title==description and evidence steps as
    # a newline string must NOT render a repeated <p> or char-per-line steps.
    b = _bug()
    b.title = b.description = "Cart total ignores the discount code"
    b.steps_to_reproduce = ["Add item to cart", "Apply code SAVE10", "Total unchanged"]
    html = Reporter()._build_html(ExplorationResult(
        url="u", bugs=[b], pages_visited=[], actions_taken=0, duration_seconds=0.0, focus_areas=[]))
    assert html.count("Cart total ignores the discount code") == 1  # title only, no repeat <p>
    assert "Apply code SAVE10" in html


def test_report_steps_no_double_numbering_and_repro_wording():
    from argus.reporter import _format_steps, _repro_detail
    # a leading "N." the model left is stripped so the <ol> numbers once
    h = _format_steps(["1. Navigate to /new", "2. Click Save"])
    assert "1. 1." not in ("<ol>" + h) and h.count("<li>") == 2
    # repro line is a plain sentence with whitespace collapsed, no "expected X absent"
    rd = _repro_detail({"attempted": True, "reproduced": True,
                        "target_text": "8\nTotal", "expect": "present", "at_url": "/tasks"})
    assert "8 Total" in rd and "expected" not in rd and "is present on the page" in rd
    rd2 = _repro_detail({"attempted": True, "reproduced": True,
                         "target_text": "Buy groceries", "expect": "absent"})
    assert "is absent from the page" in rd2


def test_short_truncates_on_word_boundary():
    assert m._short("Task creation never persists it", 20).endswith("…")
    assert " ".join(m._short("alpha beta gamma delta", 12).replace("…", "").split()) in "alpha beta gamma delta"


def test_looks_logged_out_heuristic():
    assert m._looks_logged_out(make_page_state(page_text="Please log in to continue"))
    assert m._looks_logged_out(make_page_state(page_text="Your session has expired"))
    # a logged-in page with a Login/Log out control must NOT read as logged out
    assert not m._looks_logged_out(make_page_state(page_text="Welcome back, Alice  Log out"))
