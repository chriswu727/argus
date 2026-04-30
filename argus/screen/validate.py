"""Read-only validation of screen-mode against real Mac apps.

`python -m argus.screen.validate` walks the AX tree of one or more
running apps and reports element counts + sample resolutions. It does
NOT click or type — that would seize the user's mouse / keyboard. The
clickability path is exercised in #28's verify script with a stub
backend.

Goal of this script: give a credible "Argus screen mode actually sees
inside Finder / Safari / Notes" demonstration that anyone can re-run
without risking their work.

Usage:
    python -m argus.screen.validate                # walk frontmost app
    python -m argus.screen.validate Finder Notes   # walk a list of apps
    python -m argus.screen.validate --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .backend import ScreenBackend
from .permissions import gate_screen_mode
from ..resolver import resolve_screen_element


@dataclass
class AppValidation:
    name: str
    pid: int
    window_title: str
    elements_observed: int
    by_role: dict
    sample_elements: List[dict] = field(default_factory=list)
    sample_resolutions: List[dict] = field(default_factory=list)
    screenshot: Optional[str] = None
    error: Optional[str] = None


async def validate_app(name: Optional[str]) -> AppValidation:
    backend = ScreenBackend()
    try:
        obs = await backend.start(target_app=name)
    except Exception as exc:
        return AppValidation(
            name=name or "<frontmost>",
            pid=-1,
            window_title="",
            elements_observed=0,
            by_role={},
            error=str(exc),
        )

    by_role: dict = {}
    for el in obs.elements:
        by_role[el.role] = by_role.get(el.role, 0) + 1

    # Dump the first ten "labeled" elements as a real demonstration that
    # Argus sees inside the app. Skipping unlabeled containers — those
    # are noise here even though they're useful for resolution.
    sample_elements: List[dict] = []
    for el in obs.elements:
        label = el.title or el.value or el.description or el.role_description
        if not label:
            continue
        sample_elements.append({
            "role": el.role,
            "label": str(label)[:80],
            "rect": [el.x, el.y, el.width, el.height],
            "enabled": el.enabled,
        })
        if len(sample_elements) >= 10:
            break

    # Probe the resolver with the most generic labels we just found —
    # tests round-trip identity between observation and resolution. This
    # is more honest than English-only probes against a localised system.
    samples: List[dict] = []
    for el_data in sample_elements[:5]:
        probe = el_data["label"][:40]
        if not probe:
            continue
        result = resolve_screen_element(probe, obs.elements)
        samples.append({
            "probe": probe,
            "outcome": result.reason,
            "matched_role": result.found.role if result.found else None,
            "matched_label": (
                (result.found.title or result.found.value or result.found.description)
                if result.found else None
            ),
        })

    return AppValidation(
        name=obs.foreground_app,
        pid=obs.foreground_pid,
        window_title=obs.foreground_window_title,
        elements_observed=len(obs.elements),
        by_role=by_role,
        sample_elements=sample_elements,
        sample_resolutions=samples,
        screenshot=obs.screenshot_path,
    )


async def run(targets: List[str]) -> dict:
    missing = gate_screen_mode()
    if missing:
        names = [c.name for c in missing]
        return {
            "ok": False,
            "error": f"Missing macOS grants: {names}. Run argus-mcp --doctor.",
        }

    started = time.time()
    if not targets:
        results = [await validate_app(None)]
    else:
        results = []
        for t in targets:
            results.append(await validate_app(t))

    return {
        "ok": True,
        "duration_s": round(time.time() - started, 2),
        "results": [asdict(r) for r in results],
    }


def render_text(report: dict) -> str:
    if not report.get("ok"):
        return f"Screen-mode validation failed: {report.get('error')}"
    lines = [f"Argus screen-mode validation"]
    lines.append(f"  duration: {report['duration_s']}s")
    lines.append("")
    for r in report["results"]:
        lines.append(f"# {r['name']}  (pid {r['pid']})")
        if r.get("error"):
            lines.append(f"  ERROR: {r['error']}")
            lines.append("")
            continue
        lines.append(f"  window: {r['window_title']!r}")
        lines.append(f"  AX-tree elements observed: {r['elements_observed']}")
        if r["by_role"]:
            top = sorted(r["by_role"].items(), key=lambda kv: -kv[1])[:8]
            lines.append("  by role:")
            for role, count in top:
                lines.append(f"    {role}: {count}")
        if r["sample_elements"]:
            lines.append("  sample observed elements (first 10 with labels):")
            for el in r["sample_elements"]:
                rect = el["rect"]
                lines.append(
                    f"    {el['role']:<18} {el['label'][:50]!r:<52} "
                    f"@ ({rect[0]},{rect[1]}) {rect[2]}x{rect[3]}"
                )
        if r["sample_resolutions"]:
            uniq = sum(1 for s in r["sample_resolutions"] if s["outcome"] == "unique")
            ambig = sum(1 for s in r["sample_resolutions"] if s["outcome"] == "ambiguous")
            other = len(r["sample_resolutions"]) - uniq - ambig
            lines.append(
                f"  round-trip identity probes: {uniq} unique / {ambig} ambiguous / "
                f"{other} no-match (ambiguous = resolver refused to guess — safe)"
            )
        if r["screenshot"]:
            lines.append(f"  screenshot: {r['screenshot']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m argus.screen.validate")
    p.add_argument("targets", nargs="*",
                   help="App names to walk (e.g. Finder Notes Safari). "
                        "Empty = frontmost app.")
    p.add_argument("--json", type=Path, default=None,
                   help="Write the report as JSON to this path.")
    args = p.parse_args()

    report = asyncio.run(run(args.targets))
    print(render_text(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nJSON written: {args.json}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
