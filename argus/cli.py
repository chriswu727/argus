import asyncio
import sys

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
@click.option("--headed", is_flag=True, help="Show browser window")
@click.option("--output", "-o", default="./argus-reports", help="Report output dir")
@click.option("--model", default="gpt-4o-mini", help="LLM model (any LiteLLM-supported: gpt-4o, claude-sonnet-4-20250514, deepseek/deepseek-chat, ollama/llama3, etc.)")
@click.option("--api-base", default=None, help="Custom API base URL (for OpenAI-compatible providers)")
@click.option("--api-key", default=None, help="API key (overrides env var)")
def main(url, config_file, focus, max_steps, headed, output, model, api_base, api_key):
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
        result = asyncio.run(_run(cfg))
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
    try:
        console.print(f"Re-testing {len(entries)} journaled finding(s) for {origin}:\n")
        for e in entries:
            r = await mcp._run_reproduction_check(sess, e.get("verify") or {})
            rep = r.get("reproduced")
            if rep is True:
                tag, color = "STILL-PRESENT", "red"
                still += 1
            elif rep is False:
                tag, color = "FIXED        ", "green"
                gone += 1
            else:
                tag, color = "INCONCLUSIVE ", "yellow"
                incon += 1
            console.print(f"  [{color}]{tag}[/] [{e.get('severity', '?').upper()}] {e.get('title', '')[:70]}")
    finally:
        try:
            await sess.browser.stop()
        except Exception:
            pass

    console.print(f"\n{still} still present · {gone} fixed · {incon} inconclusive")
    return 1 if still else 0
