"""Argus reproducible benchmark — `python -m argus.bench`.

Drives Argus's MCP tools against one or more seeded fixtures and
reports recall as a matrix. The "agent" is a fixed Python sequence
per scenario; the point is to measure Argus's *capability ceiling*
(what's findable with these tools) rather than the variability of
any one LLM.

Usage:
    # one fixture at a time
    python -m argus.bench --target buggytasks
    python -m argus.bench --target darkshop

    # both, write a matrix report
    python -m argus.bench --target all \\
        --json bench-results/matrix.json \\
        --md   bench-results/matrix.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional

from .runner import run_scenarios, BenchReport
from . import scenarios_buggytasks
from . import scenarios_darkshop


_TARGETS = {
    "buggytasks": (scenarios_buggytasks.BASE_URL, scenarios_buggytasks.SCENARIOS),
    "darkshop":   (scenarios_darkshop.BASE_URL,   scenarios_darkshop.SCENARIOS),
}


def matrix_md(reports: List[BenchReport]) -> str:
    """Cross-fixture matrix Markdown — the headline artifact."""
    lines = [
        "# Argus benchmark matrix",
        "",
        "| Fixture     | Recall            | Duration | Fixture URL                  |",
        "|-------------|-------------------|----------|------------------------------|",
    ]
    for r in reports:
        recall = f"{r.caught} / {r.total} = {r.recall * 100:.0f} %"
        dur = f"{r.finished_at - r.started_at:.1f} s"
        lines.append(
            f"| {r.target:<11} | {recall:<17} | {dur:<8} | `{r.fixture_url}` |"
        )
    lines.append("")
    lines.append(
        "Argus's MCP surface is fixture-agnostic — both BuggyTasks (mechanical "
        "bugs) and DarkShop (human-eye bugs) are exercised through the same "
        "description-keyed tools."
    )
    lines.append("")
    for r in reports:
        lines.append(f"## {r.target}")
        lines.append("")
        lines.append(r.to_markdown())
        lines.append("")
    return "\n".join(lines)


def matrix_json(reports: List[BenchReport]) -> dict:
    return {
        "matrix": [r.to_json() for r in reports],
        "totals": {
            "caught": sum(r.caught for r in reports),
            "total": sum(r.total for r in reports),
            "recall_pct": (
                round(
                    sum(r.caught for r in reports) /
                    max(1, sum(r.total for r in reports)) * 100, 1
                )
            ),
        },
    }


async def run(targets: List[str], out_json: Optional[Path], out_md: Optional[Path]) -> int:
    reports: List[BenchReport] = []
    for t in targets:
        if t not in _TARGETS:
            print(f"Unknown target: {t}. Choose from: {', '.join(_TARGETS)}.")
            return 2
        base, scenarios = _TARGETS[t]
        try:
            report = await run_scenarios(t, base, scenarios)
        except RuntimeError as exc:
            print(str(exc))
            return 2
        reports.append(report)
        print()  # blank line between fixtures

    if len(reports) > 1:
        print("=" * 72)
        print("MATRIX SUMMARY")
        print("=" * 72)
        for r in reports:
            print(
                f"  {r.target:<12} {r.caught:>3} / {r.total:<3} = "
                f"{r.recall * 100:>3.0f} %  in {r.finished_at - r.started_at:>5.1f}s"
            )

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        if len(reports) == 1:
            out_json.write_text(json.dumps(reports[0].to_json(), indent=2))
        else:
            out_json.write_text(json.dumps(matrix_json(reports), indent=2))
        print(f"\nJSON written: {out_json}")
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        if len(reports) == 1:
            out_md.write_text(reports[0].to_markdown())
        else:
            out_md.write_text(matrix_md(reports))
        print(f"Markdown written: {out_md}")

    # Return code: 0 only if every report hit 100 % recall
    return 0 if all(r.caught == r.total for r in reports) else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m argus.bench")
    p.add_argument("--target", choices=list(_TARGETS) + ["all"], default="buggytasks",
                   help="Which fixture to run against. 'all' = matrix.")
    p.add_argument("--json", type=Path, default=None,
                   help="Write the report (or matrix) as JSON.")
    p.add_argument("--md", type=Path, default=None,
                   help="Write the report (or matrix) as Markdown.")
    args = p.parse_args()

    if args.target == "all":
        targets = list(_TARGETS.keys())
    else:
        targets = [args.target]

    return asyncio.run(run(targets, args.json, args.md))


if __name__ == "__main__":
    sys.exit(main())
