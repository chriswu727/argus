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
@click.option("--passes", "-p", default=1, help="Independent exploration passes; findings are UNION-ed and deduped. A single LLM pass finds a noisy ~fraction of bugs; more passes raise recall (at ~N x cost/time).")
@click.option("--headed", is_flag=True, help="Show browser window")
@click.option("--output", "-o", default="./argus-reports", help="Report output dir")
@click.option("--model", default="gpt-4o-mini", help="LLM model (any LiteLLM-supported: gpt-4o, claude-sonnet-4-20250514, deepseek/deepseek-chat, ollama/llama3, etc.)")
@click.option("--api-base", default=None, help="Custom API base URL (for OpenAI-compatible providers)")
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
        console.print(f"[dim]Union: {len(merged.bugs)} distinct finding(s) from "
                      f"{total} across {passes} passes.[/]")
    return merged


def _merge_results(results: list):
    """Union bugs across passes, deduped by structural fingerprint (not the LLM
    title, so a genuinely new bug is never collapsed). When the same bug appears in
    multiple passes, keep the PROVEN instance — recall must not cost precision."""
    from .models import ExplorationResult
    from .mcp_server import _bug_fingerprint

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
    for res in results:
        for p in (res.pages_visited or []):
            if p not in pages:
                pages.append(p)
    base = results[0]
    return ExplorationResult(
        url=base.url,
        bugs=[by_fp[fp] for fp in order],
        pages_visited=pages,
        actions_taken=sum(r.actions_taken for r in results),
        duration_seconds=sum(r.duration_seconds for r in results),
        focus_areas=base.focus_areas,
        screenshots=base.screenshots,
        timestamp=base.timestamp,
    )


@click.command()
@click.argument("url")
@click.option("--output", "-o", default="./argus-reports", help="Where the journal lives (ARGUS_OUTPUT_DIR)")
@click.option("--headed", is_flag=True, help="Show browser window")
def regression(url, output, headed):
    """Re-test journaled findings for URL's origin against the CURRENT build.

    Zero-LLM, read-only (clean GETs). Re-runs each previously-recorded finding's
    independent clean-load receipt and prints STILL-PRESENT / FIXED /
    INCONCLUSIVE. Exits non-zero if anything is STILL-PRESENT, so CI can gate on
    it — the shift-left "did my change reintroduce a known bug?" check.

    Findings are journaled when an agent session calls end_session; this command
    replays those receipts without an LLM.
    """
    code = asyncio.run(_run_regression(url, output, headless=not headed))
    sys.exit(code)


async def _run_regression(url: str, output: str, headless: bool = True) -> int:
    import os
    from urllib.parse import urlparse

    import argus.mcp_server as mcp
    from .browser import BrowserDriver

    os.environ["ARGUS_OUTPUT_DIR"] = output
    origin = urlparse(url).netloc or "default"
    entries = mcp._journal_entries(origin)
    if not entries:
        console.print(f"[dim]No journaled findings for {origin}. Nothing to regression-test.[/]")
        return 0

    sess = mcp.Session()
    sess.mode = "web"
    sess.url = url
    sess.browser = BrowserDriver(headless=headless)
    try:
        await sess.browser.start()
        await sess.browser.goto(url)
    except Exception as exc:
        console.print(f"[red]Could not load {url}:[/] {type(exc).__name__}: {exc}")
        try:
            await sess.browser.stop()
        except Exception:
            pass
        return 2

    still = gone = incon = 0
    results = []
    try:
        console.print(f"Re-testing {len(entries)} journaled finding(s) for {origin}:\n")
        for e in entries:
            r = await mcp._run_reproduction_check(sess, e.get("verify") or {})
            rep = r.get("reproduced")
            if rep is True:
                tag, color, status = "STILL-PRESENT", "red", "STILL-PRESENT"
                still += 1
            elif rep is False:
                tag, color, status = "FIXED        ", "green", "FIXED"
                gone += 1
            else:
                tag, color, status = "INCONCLUSIVE ", "yellow", "INCONCLUSIVE"
                incon += 1
            console.print(f"  [{color}]{tag}[/] [{e.get('severity', '?').upper()}] {e.get('title', '')[:70]}")
            results.append({"title": e.get("title", ""), "severity": e.get("severity", "?"),
                            "status": status, "runs": r.get("runs"),
                            "url": (e.get("verify") or {}).get("at_url") or url})
    finally:
        try:
            await sess.browser.stop()
        except Exception:
            pass

    console.print(f"\n{still} still present · {gone} fixed · {incon} inconclusive")
    art = _write_regression_artifact(output, url, results)
    if art:
        console.print(f"[dim]Machine-readable: {art}.json · {art}.junit.xml[/]")
    return 1 if still else 0


def _write_regression_artifact(output_dir: str, url: str, results: list) -> Optional[str]:
    """Emit the regression run as JSON + JUnit so CI can consume it, not just the
    exit code — STILL-PRESENT = a <failure> (a known bug came back / never got
    fixed), FIXED = a passing testcase, INCONCLUSIVE = <skipped>. Same machine-
    readable contract as the explore report. Best-effort; never breaks the run."""
    import json
    import xml.etree.ElementTree as ET
    from datetime import datetime
    try:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        still = sum(1 for r in results if r["status"] == "STILL-PRESENT")
        doc = {
            "tool": "argus-regression", "url": url,
            "generated_at": datetime.now().isoformat(),
            "summary": {"total": len(results), "still_present": still,
                        "fixed": sum(1 for r in results if r["status"] == "FIXED"),
                        "inconclusive": sum(1 for r in results if r["status"] == "INCONCLUSIVE")},
            "findings": results,
        }
        (out / f"regression_{ts}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        suite = ET.Element("testsuite", {"name": "argus-regression",
                                         "tests": str(len(results)), "failures": str(still)})
        for r in results:
            tc = ET.SubElement(suite, "testcase", {
                "classname": f"argus.regression.{r['severity']}",
                "name": f"[{r['status']}] {r['title']}"[:250]})
            if r["status"] == "STILL-PRESENT":
                f = ET.SubElement(tc, "failure", {"message": "known bug still present", "type": "regression"})
                f.text = f"{r['title']}\nruns={r.get('runs')}\nurl={r.get('url')}"
            elif r["status"] == "INCONCLUSIVE":
                ET.SubElement(tc, "skipped", {"message": "symptom check inconclusive"})
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")
        (out / f"regression_{ts}.junit.xml").write_text(xml, encoding="utf-8")
        return str(out / f"regression_{ts}")
    except Exception:
        return None
