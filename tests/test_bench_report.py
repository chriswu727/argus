"""Tests for BenchReport's recall + false-positive-resistance accounting.

Recall measures whether seeded real bugs are found; FP-resistance measures
whether the reproduction receipt refuses tempting-but-false symptoms. A bench
that only reported recall was structurally blind to the spurious-bug rate the
differentiation pitch is built on (F10).
"""
from __future__ import annotations

from argus.bench.runner import BenchReport, ScenarioResult, receipt_rejected
from argus.models import Bug, BugType, Severity


def _report(results):
    r = BenchReport(target="t", fixture_url="u", started_at=0.0, finished_at=1.0)
    r.results = results
    return r


def _recall(caught):
    return ScenarioResult(bug_id=1, name="n", caught=caught, method="m", kind="recall")


def _fp(resisted):
    return ScenarioResult(bug_id=1, name="n", caught=resisted, method="m", kind="fp")


def test_recall_scoped_to_recall_scenarios_only():
    r = _report([_recall(True), _recall(False), _fp(True), _fp(True)])
    assert r.caught == 1 and r.total == 2
    assert r.recall == 0.5
    # FP scenarios must not dilute or inflate recall.
    assert r.fp_total == 2 and r.fp_resisted == 2
    assert r.fp_resistance == 1.0


def test_fp_resistance_counts_leaks():
    r = _report([_recall(True), _fp(True), _fp(False)])
    assert r.fp_resisted == 1 and r.fp_total == 2
    assert r.fp_resistance == 0.5


def test_passed_requires_full_recall_and_full_fp_resistance():
    assert _report([_recall(True), _fp(True)]).passed is True
    assert _report([_recall(True), _fp(False)]).passed is False  # a leak fails
    assert _report([_recall(False), _fp(True)]).passed is False  # a miss fails


def test_no_fp_scenarios_means_perfect_resistance_and_unchanged_recall():
    r = _report([_recall(True), _recall(True)])
    assert r.fp_total == 0 and r.fp_resistance == 1.0
    assert r.recall == 1.0 and r.passed is True


def test_to_json_exposes_precision_fields():
    j = _report([_recall(True), _fp(False)]).to_json()
    assert j["recall_pct"] == 100.0
    assert j["fp_resisted"] == 0 and j["fp_total"] == 1
    assert j["fp_resistance_pct"] == 0.0
    assert {res["kind"] for res in j["results"]} == {"recall", "fp"}


def _bug_with_receipt(receipt):
    return Bug(type=BugType.UX_ISSUE, severity=Severity.LOW, title="t",
               description="d", url="u", steps_to_reproduce=[],
               reproduction_receipt=receipt)


def test_receipt_rejected_only_for_attempted_unconfirmed():
    assert receipt_rejected(_bug_with_receipt(
        {"attempted": True, "reproduced": False})) is True
    assert receipt_rejected(_bug_with_receipt(
        {"attempted": True, "reproduced": True})) is False
    assert receipt_rejected(_bug_with_receipt(
        {"attempted": True, "reproduced": None})) is False  # nav error, not a clean reject
    assert receipt_rejected(_bug_with_receipt(None)) is False
    assert receipt_rejected(_bug_with_receipt(
        {"attempted": False, "reason": "bad verify"})) is False
