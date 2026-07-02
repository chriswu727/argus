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


def test_looks_logged_out_heuristic():
    assert m._looks_logged_out(make_page_state(page_text="Please log in to continue"))
    assert m._looks_logged_out(make_page_state(page_text="Your session has expired"))
    # a logged-in page with a Login/Log out control must NOT read as logged out
    assert not m._looks_logged_out(make_page_state(page_text="Welcome back, Alice  Log out"))
