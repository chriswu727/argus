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


def test_file_event_bugs_tags_auto_captured():
    sess = m.Session()
    bug = Bug(type=BugType.CONSOLE_ERROR, severity=Severity.HIGH, title="t",
              description="d", url="u", steps_to_reproduce=[])
    m._file_event_bugs(sess, [bug])
    assert sess.bugs == [bug]
    assert bug.reproduction_receipt["auto_captured"] is True
    assert bug.reproduction_receipt["attempted"] is False
