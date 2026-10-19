import asyncio
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from .config import Config
from .explorer import Explorer
from .reporter import Reporter

console = Console()


@click.command()
@click.argument("url")
@click.option("--config", "-c", "config_file", help="YAML config with focus areas")
@click.option("--focus", "-f", multiple=True, help="Focus area (repeatable)")
@click.option("--max-steps", "-n", default=50, help="Max exploration steps")
@click.option(
    "--passes",
    "-p",
    default=1,
    help="Independent exploration passes; findings are UNION-ed and deduped. A single LLM pass finds a noisy ~fraction of bugs; more passes raise recall (at ~N x cost/time).",
)
@click.option("--headed", is_flag=True, help="Show browser window")
@click.option("--output", "-o", default="./argus-reports", help="Report output dir")
@click.option(
    "--model",
    default="gpt-4o-mini",
    help="LLM model (any LiteLLM-supported: gpt-4o, claude-sonnet-4-20250514, deepseek/deepseek-chat, ollama/llama3, etc.)",
)
@click.option(
    "--api-base", default=None, help="Custom API base URL (for OpenAI-compatible providers)"
)
@click.option("--api-key", default=None, help="API key (overrides env var)")
def main(url, config_file, focus, max_steps, passes, headed, output, model, api_base, api_key):
    """Argus — AI-powered exploratory QA agent (CLI / LLM-planner mode).

    Give it a URL and Argus drives a Playwright browser using a LiteLLM-
    backed planner. Requires an API key for the chosen provider.

    The recommended path for most users is the MCP server (argus-mcp +
    Claude Code / Cursor) — see the README. CLI mode is kept around for
    headless / scripted runs that need their own LLM.

    \b
    Examples:
        argus http://localhost:3000
        argus http://localhost:3000 -f "test login flow" -f "try edge cases on forms"
        argus http://localhost:3000 -c focus.yaml --headed
    """
    if config_file:
        cfg = Config.from_yaml(config_file, url=url)
    else:
        cfg = Config.from_args(
            url=url,
            focus=list(focus) if focus else None,
            max_steps=max_steps,
            headless=not headed,
            output_dir=output,
            model=model,
            api_base=api_base,
            api_key=api_key,
        )

    try:
        result = asyncio.run(_run_passes(cfg, max(1, passes)))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Argus stopped exploring.[/]")
        sys.exit(130)
    except Exception as exc:
        # Friendly hint by exception class name. Anything we can't classify
        # falls through to a single-line summary — never a wall of trace.
        console.print(f"\n[red]Argus failed:[/] {type(exc).__name__}: {exc}")
        msg = str(exc).lower()
        if "playwright" in msg or "browser" in msg or "net::err" in msg:
            console.print(
                "[dim]The browser couldn't reach the URL. Check that the "
                "site is up and the URL is reachable from this machine.[/]"
            )
        elif "api key" in msg or "authentication" in msg or "unauthorized" in msg or "401" in msg:
            console.print(
                "[dim]LLM auth failed. Set OPENAI_API_KEY / ANTHROPIC_API_KEY / "
                "etc., or pass --api-key explicitly.[/]"
            )
        elif "timeout" in msg:
            console.print(
                "[dim]Something timed out — the page may be slow, or the LLM "
                "provider may be rate-limiting.[/]"
            )
        sys.exit(1)

    reporter = Reporter()
    report_path = reporter.generate(result, cfg.output_dir)
    console.print(f"\n[bold green]Report saved:[/] {report_path}")


async def _run(cfg: Config):
    explorer = Explorer(cfg)
    return await explorer.run()


async def _run_passes(cfg: Config, passes: int):
    """Run N independent exploration passes and UNION their findings. A single LLM
    pass finds a noisy fraction of an app's bugs and misses different ones each time;
    unioning independent passes is the cheapest reliable recall lift (the bench
    already scores union-over-trials — this brings it into the product)."""
    results = []
    for i in range(passes):
        if passes > 1:
            console.print(f"[dim]— pass {i + 1}/{passes} —[/]")
        results.append(await _run(cfg))
    merged = _merge_results(results)
    if passes > 1:
        total = sum(len(r.bugs) for r in results)
        console.print(
            f"[dim]Union: {len(merged.bugs)} distinct finding(s) from "
            f"{total} across {passes} passes.[/]"
        )
    return merged


def _merge_results(results: list):
    """Union bugs across passes, deduped by structural fingerprint (not the LLM
    title, so a genuinely new bug is never collapsed). When the same bug appears in
    multiple passes, keep the PROVEN instance — recall must not cost precision."""
    from .application.evidence import _bug_fingerprint
    from .models import ExplorationResult

    if len(results) == 1:
        return results[0]

    def _verified(b) -> bool:
        return (b.reproduction_receipt or {}).get("reproduced") is True

    by_fp: dict = {}
    order: list = []
    for res in results:
        for b in res.bugs:
            fp = _bug_fingerprint(b)
            if fp not in by_fp:
                by_fp[fp] = b
                order.append(fp)
            elif _verified(b) and not _verified(by_fp[fp]):
                by_fp[fp] = b  # upgrade to the proven instance
    pages: list = []
    screenshots: list = []
    observations: list = []
    for res in results:
        for p in res.pages_visited or []:
            if p not in pages:
                pages.append(p)
        screenshots.extend(res.screenshots or [])
        observations.extend(res.observations or [])
    base = results[0]
    return ExplorationResult(
        url=base.url,
        bugs=[by_fp[fp] for fp in order],
        pages_visited=pages,
        actions_taken=sum(r.actions_taken for r in results),
        duration_seconds=sum(r.duration_seconds for r in results),
        focus_areas=base.focus_areas,
        screenshots=screenshots,
        observations=observations,
        tool_calls=sum(r.tool_calls for r in results),
        review_mode=base.review_mode,
        timestamp=base.timestamp,
        run_id=base.run_id,
    )


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
