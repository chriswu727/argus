"""Reproduction-receipt badge rendering + auto-capture tagging.

The badge is the only place the report tells a reader whether a finding was
independently re-confirmed. It must distinguish: verified, not-reproduced,
intermittent, auto-captured event (not verified), verify-clause-rejected, and
observation-based (no badge). Conflating any of these is the false-confidence
the precision moat exists to avoid (F9, F24, F25).
"""
from __future__ import annotations

import argus.mcp_server as m
from argus.models import Bug, BugType, Severity
from argus.reporter import _repro_badge


def test_observation_based_has_no_badge():
    assert _repro_badge(None) == ""


def test_verified_badge():
    b = _repro_badge({"attempted": True, "reproduced": True, "runs": "2/2"})
    assert "VERIFIED" in b


def test_not_reproduced_badge_is_not_discouraging_absolute():
    b = _repro_badge({"attempted": True, "reproduced": False, "flaky": False})
    assert "NOT REPRODUCED" in b
    assert "intermittent" in b.lower()  # invites re-check rather than dismissing


def test_intermittent_badge():
    b = _repro_badge({"attempted": True, "reproduced": False, "flaky": True, "runs": "1/2"})
    assert "INTERMITTENT" in b


def test_auto_captured_event_badge_is_distinct():
    b = _repro_badge({"attempted": False, "auto_captured": True})
    assert "AUTO-CAPTURED" in b
    assert "not independently verified" in b


def test_rejected_verify_clause_badge_is_distinct():
    # attempted False WITHOUT auto_captured == a verify clause that was rejected;
    # must read differently from both an auto-captured event and a no-badge bug.
    b = _repro_badge({"attempted": False, "reason": "bad verify clause"})
    assert "VERIFY NOT RUN" in b


def test_inconclusive_badge_surfaces_reason_instead_of_unknown_error():
    b = _repro_badge({
        "attempted": True,
        "reproduced": None,
        "reason": "clean load returned HTTP 404",
    })
    assert "INCONCLUSIVE" in b
    assert "HTTP 404" in b
    assert "errored" not in b.lower()


def test_file_event_bugs_tags_auto_captured():
    sess = m.Session()
    bug = Bug(type=BugType.CONSOLE_ERROR, severity=Severity.HIGH, title="t",
              description="d", url="u", steps_to_reproduce=[])
    m._file_event_bugs(sess, [bug])
    assert sess.bugs == [bug]
    assert bug.reproduction_receipt["auto_captured"] is True
    assert bug.reproduction_receipt["attempted"] is False


def test_file_event_bugs_attaches_console_and_network_evidence_to_manual_root_cause():
    missing = "https://example.test/articles/missing"
    sess = m.Session()
    manual = Bug(
        type=BugType.BROKEN_LINK,
        severity=Severity.MEDIUM,
        title="Search result opens a missing article",
        description="The result navigates to a 404.",
        url=missing,
        steps_to_reproduce=["Open search", "Click result"],
    )
    network = Bug(
        type=BugType.NETWORK_ERROR,
        severity=Severity.MEDIUM,
        title="HTTP 404",
        description="GET returned 404",
        url=missing,
        steps_to_reproduce=[],
        network_logs=[{"method": "GET", "url": missing, "status": 404, "page_url": missing}],
    )
    console = Bug(
        type=BugType.CONSOLE_ERROR,
        severity=Severity.MEDIUM,
        title="Console error: resource status 404",
        description="Failed to load resource: status 404",
        url=missing,
        steps_to_reproduce=[],
        console_logs=["Failed to load resource: status 404"],
    )
    sess.bugs = [manual]

    filed = m._file_event_bugs(sess, [console, network])

    assert filed == []
    assert sess.bugs == [manual]
    assert manual.network_logs == network.network_logs
    assert manual.console_logs == console.console_logs


def test_file_event_bugs_collapses_matching_console_and_network_events():
    missing = "https://example.test/missing"
    sess = m.Session()
    network = Bug(
        type=BugType.NETWORK_ERROR,
        severity=Severity.MEDIUM,
        title="HTTP 404",
        description="GET returned 404",
        url=missing,
        steps_to_reproduce=[],
        network_logs=[{"method": "GET", "url": missing, "status": 404, "page_url": missing}],
    )
    console = Bug(
        type=BugType.CONSOLE_ERROR,
        severity=Severity.MEDIUM,
        title="Console error: resource status 404",
        description="Failed to load resource: status 404",
        url=missing,
        steps_to_reproduce=[],
        console_logs=["Failed to load resource: status 404"],
    )

    filed = m._file_event_bugs(sess, [console, network])

    assert filed == [network]
    assert sess.bugs == [network]
    assert network.console_logs == console.console_logs
