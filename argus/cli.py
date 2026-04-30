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
