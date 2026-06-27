"""Tests for the live LLM bench (argus.bench.live).

A scripted `FakeModel` stands in for a real LLM, so the full driver loop,
tool dispatch, LLM-as-judge, and scoring are exercised with no network call and
no API key. The one end-to-end driver test runs against a self-contained
file:// page and skips when Chromium can't launch.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import argus.mcp_server as m
from argus.bench import live
from argus.bench import scenarios_darkshop as ds


# ── A scripted model ────────────────────────────────────────────────


class FakeModel:
    """Returns pre-scripted Completions in order. `name` for reporting."""

    def __init__(self, completions):
        self.name = "fake/model"
        self._queue = list(completions)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if self._queue:
            return self._queue.pop(0)
        # Default: no tool calls -> driver treats as "done".
        return live.Completion(content="done", tool_calls=[])


def _tc(name, args, cid="c1"):
    return live.ToolCall(id=cid, name=name, arguments=args)


# ── tool-schema plumbing ────────────────────────────────────────────


def test_build_tool_schemas_covers_allowlist_with_real_params():
    schemas = live.build_tool_schemas(["observe", "record_bug", "click_what"])
    names = [s["function"]["name"] for s in schemas]
    assert names == ["observe", "record_bug", "click_what"]
    record = next(s for s in schemas if s["function"]["name"] == "record_bug")
    params = record["function"]["parameters"]
    assert params["type"] == "object"
    # record_bug's first two args are required; the schema is FastMCP's own.
    assert "title" in params["properties"] and "severity" in params["properties"]


def test_build_tool_schemas_skips_unknown_tool_names():
    schemas = live.build_tool_schemas(["observe", "does_not_exist"])
    assert [s["function"]["name"] for s in schemas] == ["observe"]


def test_default_web_tools_are_all_real_and_exclude_screen():
    idx = {t.name for t in m.mcp._tool_manager.list_tools()}
    for name in live.DEFAULT_WEB_TOOLS:
        assert name in idx, f"{name} is not a registered MCP tool"
    assert not any(n.startswith("screen_") for n in live.DEFAULT_WEB_TOOLS)
    # eval_js is deliberately withheld — the live agent tests black-box.
    assert "eval_js" not in live.DEFAULT_WEB_TOOLS


# ── seeded-spec extraction ──────────────────────────────────────────


def test_seeded_specs_reuses_scenario_docstrings():
    specs = live.seeded_specs(ds.SCENARIOS)
    assert len(specs) == len(ds.SCENARIOS)
    first = specs[0]
    assert first["id"] == 1
    assert "Only 3 left" in first["name"]
    # The "BUG #1:" marker is stripped; the human description survives.
    assert "hardcoded" in first["description"].lower()
    assert not first["description"].lower().startswith("bug #")


# ── judge JSON parsing ──────────────────────────────────────────────


def test_parse_json_object_tolerates_code_fence_and_prose():
    assert live._parse_json_object('```json\n{"matches": []}\n```') == {"matches": []}
    assert live._parse_json_object('sure:\n{"matches": [{"seeded_id": 1}]} done') == {
        "matches": [{"seeded_id": 1}]
    }
    assert live._parse_json_object("no json here") == {}


# ── judge scoring + one-to-one contract ─────────────────────────────


class _Bug:
    def __init__(self, title, severity="medium", description=""):
        self.title = title
        self.severity = severity
        self.description = description


def test_judge_recall_maps_and_dedupes():
    seeded = [
        {"id": 1, "name": "Stale nav greeting", "description": "rename not reflected"},
        {"id": 2, "name": "Fake sale badge", "description": "price == original"},
    ]
    reported = [_Bug("Navbar shows old name after rename"),
                _Bug("Discount badge is bogus")]
    # Judge returns a valid mapping plus a duplicate that must be dropped.
    model = FakeModel([
        live.Completion(content='{"matches": ['
                        '{"seeded_id": 1, "reported_index": 0},'
                        '{"seeded_id": 2, "reported_index": 1},'
                        '{"seeded_id": 2, "reported_index": 0}]}',
                        tool_calls=[])
    ])
    res = live.judge_recall(model, seeded, reported)
    assert res.matched_seeded_ids == [1, 2]
    assert len(res.matches) == 2  # duplicate seeded_id=2 dropped


def test_judge_recall_rejects_out_of_range_and_unknown():
    seeded = [{"id": 5, "name": "x", "description": "y"}]
    reported = [_Bug("something")]
    model = FakeModel([
        live.Completion(content='{"matches": ['
                        '{"seeded_id": 99, "reported_index": 0},'   # unknown seeded id
                        '{"seeded_id": 5, "reported_index": 7}]}',  # bad index
                        tool_calls=[])
    ])
    res = live.judge_recall(model, seeded, reported)
    assert res.matched_seeded_ids == []


def test_judge_recall_handles_empty_reply():
    seeded = [{"id": 1, "name": "x", "description": "y"}]
    model = FakeModel([live.Completion(content="", tool_calls=[])])
    res = live.judge_recall(model, seeded, [])
    assert res.matched_seeded_ids == []


# ── report shaping ──────────────────────────────────────────────────


def test_live_report_recall_and_extras():
    seeded = [{"id": 1, "name": "A", "description": ""},
              {"id": 2, "name": "B", "description": ""}]
    run = live.LiveRun(bugs=[_Bug("found A"), _Bug("noise")],
                       turns=3, tool_calls=5, stop_reason="end_session")
    judge = live.JudgeResult(matched_seeded_ids=[1],
                             matches=[{"seeded_id": 1, "reported_index": 0}])
    rep = live.LiveReport(target="darkshop", model="fake/model",
                          fixture_url="http://x", seeded=seeded, run=run, judge=judge,
                          started_at=0.0, finished_at=2.0)
    assert rep.caught == 1 and rep.total == 2 and rep.recall == 0.5
    assert rep.extra_reported == 1  # 2 reported - 1 matched
    j = rep.to_json()
    assert j["recall_pct"] == 50.0
    assert {s["id"]: s["caught"] for s in j["seeded"]} == {1: True, 2: False}
    assert "darkshop" in rep.to_markdown()


# ── end-to-end driver loop (needs a browser) ────────────────────────


_PAGE = "<html><body><h1>Tasks</h1><ul><li>Buy groceries</li></ul></body></html>"


async def test_drive_runs_loop_records_bug_and_ends():
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(_PAGE)
    f.close()
    url = Path(f.name).as_uri()

    # Scripted agent: observe -> record_bug -> end_session.
    model = FakeModel([
        live.Completion(content=None, tool_calls=[_tc("observe", {}, "a")]),
        live.Completion(content=None, tool_calls=[_tc(
            "record_bug",
            {"title": "Groceries item looks wrong",
             "severity": "low",
             "evidence": {"screenshot": "skip"}},
            "b")]),
        live.Completion(content=None, tool_calls=[_tc("end_session", {}, "c")]),
    ])

    try:
        run = await live.drive(url, model, max_turns=10)
    except RuntimeError as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")

    assert run.stop_reason == "end_session"
    assert run.tool_calls == 3
    assert any("Groceries" in b.title for b in run.bugs)
    # Session was torn down by end_session.
    assert not m._session.active


async def test_drive_stops_at_max_turns_and_closes_session():
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
    f.write(_PAGE)
    f.close()
    url = Path(f.name).as_uri()

    # Model that never ends: always asks to observe again.
    forever = [live.Completion(content=None, tool_calls=[_tc("observe", {}, "a")])
               for _ in range(20)]
    model = FakeModel(forever)

    try:
        run = await live.drive(url, model, max_turns=3)
    except RuntimeError as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")

    assert run.stop_reason == "max_turns"
    assert run.turns == 3
    # The finally-block must close the session the agent left open.
    assert not m._session.active
