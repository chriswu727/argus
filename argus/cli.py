import asyncio
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

console = Console()


@click.command()
@click.argument("url")
@click.option(
    "--output",
    "-o",
    default=None,
    help="Journal dir (defaults to $ARGUS_OUTPUT_DIR, else ./argus-reports)",
)
@click.option("--headed", is_flag=True, help="Show browser window")
def regression(url, output, headed):
    """Re-test journaled findings for URL's origin against the CURRENT build.

    Zero-LLM, read-only (clean GETs). Re-runs each previously-recorded finding's
    independent clean-load receipt and prints STILL-PRESENT / FIXED /
    INCONCLUSIVE. Exit codes: 0 clean/no findings, 1 still present, 2 operational
    error, 3 inconclusive. Inconclusive checks cannot silently pass a CI gate.

    Findings are journaled when an agent session calls end_session; this command
    replays those receipts without an LLM.
    """
    code = asyncio.run(_run_regression(url, output, headless=not headed))
    sys.exit(code)


def _resolve_output_dir(output: Optional[str]) -> str:
    """An explicit --output wins; otherwise respect an already-exported
    ARGUS_OUTPUT_DIR (the journal was written there by end_session) before falling
    back. The old default silently overrode the env var, so a user's journal became
    invisible ('no journaled findings')."""
    import os

    return output or os.environ.get("ARGUS_OUTPUT_DIR") or "./argus-reports"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@click.command()
@click.option(
    "--output",
    "-o",
    default=None,
    help="Report directory (defaults to $ARGUS_OUTPUT_DIR, else ./argus-reports)",
)
@click.option("--keep-runs", type=click.IntRange(min=0), default=20, show_default=True)
@click.option("--older-than-days", type=click.IntRange(min=0), default=0, show_default=True)
@click.option("--max-size-mb", type=click.FloatRange(min=0), default=0.0, show_default=True)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Delete the selected run artifacts; otherwise only show the plan",
)
def cleanup(output, keep_runs, older_than_days, max_size_mb, apply_changes):
    """Plan or apply safe cleanup of complete Argus report runs.

    The newest KEEP_RUNS are always protected. Older runs are selected once
    they exceed that count; OLDER_THAN_DAYS can require a minimum age, and
    MAX_SIZE_MB can additionally trim unprotected runs to a size budget.
    Journals and state capsules under .argus are never touched.
    """
    from .cleanup import execute_cleanup, plan_cleanup

    output_dir = _resolve_output_dir(output)
    plan = plan_cleanup(
        output_dir,
        keep_runs=keep_runs,
        older_than_days=older_than_days,
        max_bytes=int(max_size_mb * 1024 * 1024),
    )
    console.print(f"Found {len(plan.runs)} report run(s), {_format_bytes(plan.total_bytes)} total.")
    if plan.legacy_screenshot_files:
        console.print(
            f"Legacy ungrouped screenshots: {plan.legacy_screenshot_files} file(s), "
            f"{_format_bytes(plan.legacy_screenshot_bytes)}; left untouched because they "
            "cannot be mapped safely to one old run."
        )
    if not plan.selected:
        console.print("Nothing is eligible for cleanup.")
        return
    console.print(f"Selected {len(plan.selected)} run(s), {_format_bytes(plan.selected_bytes)}.")
    for run in plan.selected[:10]:
        console.print(f"  {run.run_id}  {_format_bytes(run.size_bytes)}")
    if len(plan.selected) > 10:
        console.print(f"  … and {len(plan.selected) - 10} more")
    if not apply_changes:
        console.print("Dry run only. Re-run with --apply to delete these complete run artifacts.")
        return
    removed_paths, removed_bytes = execute_cleanup(plan)
    console.print(f"Removed {removed_paths} artifact path(s), {_format_bytes(removed_bytes)}.")


async def _run_regression(url: str, output: str, headless: bool = True) -> int:
    from .application.regression import run_regression
    from .bootstrap import create_runtime
    from .settings import Settings

    output = _resolve_output_dir(output)
    runtime = create_runtime(Settings.from_env(output_dir=Path(output)))
    run = await run_regression(runtime, url, headless)
    if run.error:
        console.print(f"[red]{run.error}[/]")
        return run.exit_code
    for result in run.results:
        console.print(f"{result['status']} [{result['severity'].upper()}] {result['title'][:70]}")
    if not run.results:
        console.print("No journaled findings. Nothing to regression-test.")
        return 0
    art = _write_regression_artifact(output, url, run.results)
    if art:
        console.print(f"[dim]Machine-readable: {art}.json · {art}.junit.xml[/]")
    else:
        console.print("[red]Regression artifacts could not be saved.[/]")
        return 2
    return run.exit_code


def _write_regression_artifact(output_dir: str, url: str, results: list) -> Optional[str]:
    """Emit the regression run as JSON + JUnit so CI can consume it, not just the
    exit code — STILL-PRESENT = a <failure> (a known bug came back / never got
    fixed), FIXED = a passing testcase, INCONCLUSIVE = <skipped>. Same machine-
    readable contract as the explore report. Returns None on storage failure."""
    import xml.etree.ElementTree as ET
    from datetime import datetime

    from .domain.models import new_run_id
    from .storage import atomic_write_json, atomic_write_text, storage_warning

    try:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = new_run_id()
        still = sum(1 for r in results if r["status"] == "STILL-PRESENT")
        doc = {
            "schema_version": 1,
            "tool": "argus-regression",
            "url": url,
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total": len(results),
                "still_present": still,
                "fixed": sum(1 for r in results if r["status"] == "FIXED"),
                "inconclusive": sum(1 for r in results if r["status"] == "INCONCLUSIVE"),
            },
            "findings": results,
        }
        atomic_write_json(out / f"regression_{ts}.json", doc)
        suite = ET.Element(
            "testsuite",
            {"name": "argus-regression", "tests": str(len(results)), "failures": str(still)},
        )
        for r in results:
            tc = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": f"argus.regression.{r['severity']}",
                    "name": f"[{r['status']}] {r['title']}"[:250],
                },
            )
            if r["status"] == "STILL-PRESENT":
                f = ET.SubElement(
                    tc, "failure", {"message": "known bug still present", "type": "regression"}
                )
                f.text = f"{r['title']}\nruns={r.get('runs')}\nurl={r.get('url')}"
            elif r["status"] == "INCONCLUSIVE":
                ET.SubElement(tc, "skipped", {"message": "symptom check inconclusive"})
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")
        atomic_write_text(out / f"regression_{ts}.junit.xml", xml)
        return str(out / f"regression_{ts}")
    except Exception as exc:
        storage_warning(f"could not save regression artifacts: {type(exc).__name__}")
        return None
