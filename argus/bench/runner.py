"""Bench runner — fixture-agnostic harness.

Owns the report-building, the timing, the fixture health-check, and
the loop over scenarios. The *scenarios* live in fixture-specific
modules (`scenarios_buggytasks`, `scenarios_darkshop`) — each module
exposes a `BASE_URL` and a `SCENARIOS` list.

Bench is intentionally simple: scripted competent-agent runs.
A `--agent <model>` mode (real LLM driving the same MCP tools) will
plug in here later by exposing the same `(bug_id, name, fn)` shape.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

import argus.mcp_server as mcp_module
from argus.models import Bug


# eval_js needs --unsafe — bench enables it.
os.environ.setdefault("ARGUS_UNSAFE_EVAL", "1")


# ── Result types ────────────────────────────────────────────────────


@dataclass
class ScenarioResult:
    bug_id: int
    name: str
    caught: bool  # recall: bug found. fp: false symptom correctly resisted.
    method: str  # "auto-event" | "agent-record" | "fp-resisted" | "skipped" | "error"
    notes: str = ""
    elapsed_s: float = 0.0
    kind: str = "recall"  # "recall" (seeded real bug) | "fp" (false-positive bait)


@dataclass
class BenchReport:
    target: str
    fixture_url: str
    started_at: float
    finished_at: float
    results: List[ScenarioResult] = field(default_factory=list)

    # Recall is measured over seeded real bugs only; FP-bait scenarios are
    # scored separately as false-positive resistance (the precision side of
    # the moat). A bench that only reports recall is structurally blind to the
    # spurious-bug rate the differentiation pitch is built on.
    @property
    def _recall_results(self) -> List[ScenarioResult]:
        return [r for r in self.results if r.kind == "recall"]

    @property
    def _fp_results(self) -> List[ScenarioResult]:
        return [r for r in self.results if r.kind == "fp"]

    @property
    def caught(self) -> int:
        return sum(1 for r in self._recall_results if r.caught)

    @property
    def total(self) -> int:
        return len(self._recall_results)

    @property
    def recall(self) -> float:
        return (self.caught / self.total) if self.total else 0.0

    @property
    def fp_resisted(self) -> int:
        return sum(1 for r in self._fp_results if r.caught)

    @property
    def fp_total(self) -> int:
        return len(self._fp_results)

    @property
    def fp_resistance(self) -> float:
        """Fraction of false-positive baits the receipt refused to confirm.
        1.0 when there are no FP scenarios (nothing to get wrong)."""
        return (self.fp_resisted / self.fp_total) if self.fp_total else 1.0

    @property
    def passed(self) -> bool:
        """Bench passes only if recall is complete AND every FP bait resisted."""
        return self.caught == self.total and self.fp_resisted == self.fp_total

    def to_json(self) -> dict:
        return {
            "target": self.target,
            "fixture": self.fixture_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 2),
            "caught": self.caught,
            "total": self.total,
            "recall_pct": round(self.recall * 100, 1),
            "fp_resisted": self.fp_resisted,
            "fp_total": self.fp_total,
            "fp_resistance_pct": round(self.fp_resistance * 100, 1),
            "results": [
                {
                    "bug_id": r.bug_id,
                    "name": r.name,
                    "kind": r.kind,
                    "caught": r.caught,
                    "method": r.method,
                    "notes": r.notes,
                    "elapsed_s": round(r.elapsed_s, 2),
                }
                for r in self.results
            ],
        }

    def to_markdown(self) -> str:
        # Skip the Notes column when no row has anything to put in it.
        show_notes = any(r.notes.strip() for r in self.results)
        lines = [
            f"# Argus benchmark — {self.target}",
            "",
            f"- Fixture: `{self.fixture_url}`",
            f"- Duration: {self.finished_at - self.started_at:.1f} s",
            f"- **Recall: {self.caught} / {self.total} "
            f"= {self.recall * 100:.0f} %**",
        ]
        if self.fp_total:
            lines.append(
                f"- **FP-resistance: {self.fp_resisted} / {self.fp_total} "
                f"= {self.fp_resistance * 100:.0f} %** "
                f"(false symptoms the receipt refused to confirm)"
            )
        lines.append("")
        if show_notes:
            lines.append(
                "| #  | Seeded bug                                              "
                "| Caught | Method        | Notes                          |"
            )
            lines.append(
                "|----|---------------------------------------------------------"
                "|--------|---------------|--------------------------------|"
            )
        else:
            lines.append(
                "| #  | Seeded bug                                              "
                "| Caught | Method        |"
            )
            lines.append(
                "|----|---------------------------------------------------------"
                "|--------|---------------|"
            )
        for r in self.results:
            mark = "yes" if r.caught else "no"
            if show_notes:
                lines.append(
                    f"| {r.bug_id:>2} | {r.name[:55]:<55} | {mark:<6} | "
                    f"{r.method:<13} | {r.notes[:30]:<30} |"
                )
            else:
                lines.append(
                    f"| {r.bug_id:>2} | {r.name[:55]:<55} | {mark:<6} | "
                    f"{r.method:<13} |"
                )
        return "\n".join(lines)


# ── Shared helpers used by every scenario ──────────────────────────


async def call(tool, *args, **kwargs):
    """Invoke an MCP tool's underlying coroutine. Both raw functions and
    FastMCP-wrapped tools (.fn) work."""
    fn = getattr(tool, "fn", tool)
    return await fn(*args, **kwargs)


async def reset(mode: str = "seeded") -> None:
    """POST /api/test/reset?mode=... on whatever fixture this session is on."""
    res = await call(
        mcp_module.eval_js,
        code=(
            f"() => fetch('/api/test/reset?mode={mode}', "
            f"{{method:'POST'}}).then(r => r.json())"
        ),
    )
    if "ok" not in res.lower():
        raise RuntimeError(f"reset({mode!r}) failed: {res}")


def bugs_added_since(s, before_count: int) -> List[Bug]:
    return s.bugs[before_count:]


def records_match(bugs: List[Bug], substrs: List[str]) -> bool:
    """Did any new Bug's title or description mention any of the expected substrs?"""
    for b in bugs:
        hay = (b.title + " " + b.description).lower()
        if any(sub.lower() in hay for sub in substrs):
            return True
    return False


def receipt_rejected(bug: Bug) -> bool:
    """True if the bug's reproduction receipt refused to confirm the symptom.

    The FP-resistance scenarios deliberately file a tempting-but-false symptom
    with a verify clause; the moat is working iff the receipt comes back
    reproduced=False (UNCONFIRMED), i.e. no false VERIFIED was emitted.
    """
    r = bug.reproduction_receipt
    return bool(r) and r.get("attempted") is True and r.get("reproduced") is False


# ── Fixture health-check ────────────────────────────────────────────


def fixture_healthy(base_url: str) -> Optional[str]:
    """Return None if the fixture's /api/test/state responds, else a
    human-readable error string."""
    state_url = base_url.rstrip("/") + "/api/test/state"
    try:
        with urllib.request.urlopen(state_url, timeout=3) as r:
            if r.status != 200:
                return f"GET {state_url} returned HTTP {r.status}"
            payload = r.read()
            if b"{" not in payload:
                return f"{state_url} did not return JSON"
            return None
    except Exception as exc:
        return f"could not reach {base_url} ({exc})"


# ── Runner ──────────────────────────────────────────────────────────


ScenarioFn = Callable[[object], Awaitable[Tuple[bool, str]]]
ScenarioTuple = Tuple[int, str, ScenarioFn]


async def run_scenarios(
    target: str,
    base_url: str,
    scenarios: List[ScenarioTuple],
    out_json: Optional[Path] = None,
    out_md: Optional[Path] = None,
) -> BenchReport:
    err = fixture_healthy(base_url)
    if err:
        raise RuntimeError(
            f"Argus bench: fixture `{target}` not ready — {err}\n"
            f"  start it with: python {target_to_app_hint(target)}"
        )

    started = time.time()
    print(f"Argus bench — {target}: {base_url}")
    print(f"Running {len(scenarios)} scenarios...\n")

    await call(mcp_module.start_session, base_url + ("/" if not base_url.endswith("/") else ""))
    s = mcp_module._session

    report = BenchReport(
        target=target, fixture_url=base_url,
        started_at=started, finished_at=started,
    )

    for scenario in scenarios:
        bug_id, name, fn = scenario[:3]
        kind = scenario[3] if len(scenario) > 3 else "recall"
        t0 = time.time()
        err_str = None
        try:
            caught, method = await fn(s)
        except Exception as exc:
            caught, method = False, "error"
            err_str = repr(exc)
        elapsed = time.time() - t0
        report.results.append(ScenarioResult(
            bug_id=bug_id, name=name, caught=caught, method=method,
            notes=(err_str or "")[:60], elapsed_s=elapsed, kind=kind,
        ))
        mark = "[Y]" if caught else "[ ]"
        suffix = f" ({err_str})" if err_str else ""
        print(f"  {mark} #{bug_id:>2}  {name[:60]:<60} {elapsed:5.1f}s{suffix}")

    await call(mcp_module.end_session)
    report.finished_at = time.time()

    print(f"\nRecall: {report.caught} / {report.total} = "
          f"{report.recall * 100:.0f} %")
    if report.fp_total:
        print(f"FP-resistance: {report.fp_resisted} / {report.fp_total} = "
              f"{report.fp_resistance * 100:.0f} %")
    print(f"Duration: {report.finished_at - report.started_at:.1f} s")

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report.to_json(), indent=2))
        print(f"JSON written: {out_json}")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(report.to_markdown())
        print(f"Markdown written: {out_md}")

    return report


def target_to_app_hint(target: str) -> str:
    return {
        "buggytasks": "test-site/app.py",
        "darkshop": "human-eye-fixture/app.py",
    }.get(target, "<fixture>")
