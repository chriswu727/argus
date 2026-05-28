"""Tests for the reproduction-receipt layer — Argus's anti-false-positive guard.

`_receipt_verdict` and `_resolve_url` are pure and tested directly. The
end-to-end path (record_bug independently re-confirming a symptom on a clean
load) runs against a self-contained file:// page and is skipped when Chromium
isn't installed.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import argus.mcp_server as m


# ── pure verdict logic ───────────────────────────────────────────────

def test_verdict_all_runs_match_is_reproduced():
    r = m._receipt_verdict([True, True], "present")
    assert r["reproduced"] is True and r["flaky"] is False and r["runs"] == "2/2"


def test_verdict_no_runs_match_is_not_reproduced():
    r = m._receipt_verdict([True, True], "absent")  # claimed absent, seen present
    assert r["reproduced"] is False and r["flaky"] is False and r["runs"] == "0/2"


def test_verdict_partial_match_is_flaky():
    r = m._receipt_verdict([True, False], "present")
    assert r["reproduced"] is False and r["flaky"] is True and r["runs"] == "1/2"


def test_verdict_absent_claim_confirmed_when_text_gone():
    r = m._receipt_verdict([False, False], "absent")
    assert r["reproduced"] is True and r["runs"] == "2/2"


# ── relative-URL resolution ──────────────────────────────────────────

class _FakePage:
    def __init__(self, url):
        self.url = url


class _FakeBrowser:
    def __init__(self, url):
        self._page = _FakePage(url)


class _FakeSession:
    def __init__(self, url):
        self.browser = _FakeBrowser(url)


def test_resolve_url_joins_relative_against_current_origin():
    s = _FakeSession("http://127.0.0.1:5555/account")
    assert m._resolve_url(s, "/tasks") == "http://127.0.0.1:5555/tasks"


def test_resolve_url_passes_absolute_through():
    s = _FakeSession("http://127.0.0.1:5555/account")
    assert m._resolve_url(s, "http://other.test/x") == "http://other.test/x"


# ── end-to-end: independent re-confirmation on a clean load ──────────

_PAGE = "<html><body><h1>Tasks</h1><ul><li>Buy groceries</li></ul></body></html>"


async def _session_on_page(html: str):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(html)
    f.close()
    url = Path(f.name).as_uri()
    try:
        await m.start_session.fn(url) if hasattr(m.start_session, "fn") else await m.start_session(url)
    except Exception as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")
    return url


async def _record(**kw):
    fn = getattr(m.record_bug, "fn", m.record_bug)
    return await fn(**kw)


async def _end():
    fn = getattr(m.end_session, "fn", m.end_session)
    await fn()


async def test_receipt_confirms_true_symptom_and_flags_false_one():
    await _session_on_page(_PAGE)
    try:
        # Real symptom present on the page -> reproduced.
        await _record(title="real", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "Buy groceries"})
        # Claimed present but not on the page -> must NOT reproduce.
        await _record(title="bogus", severity="low", evidence={"screenshot": "skip"},
                      verify={"expect": "present", "target_text": "ZZZ-NOT-REAL"})
        # No verify clause -> observation-based, receipt stays None.
        await _record(title="visual", severity="low", evidence={"screenshot": "skip"})

        s = getattr(m, "_session", None) or m._require_session()
        by_title = {b.title: b for b in s.bugs}
        assert by_title["real"].reproduction_receipt["reproduced"] is True
        assert by_title["bogus"].reproduction_receipt["reproduced"] is False
        assert by_title["visual"].reproduction_receipt is None
    finally:
        await _end()
