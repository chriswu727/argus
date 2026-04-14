import asyncio

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
    """Argus — AI-powered exploratory QA agent.

    Give it a URL and it explores your app like a real user, finding bugs
    that scripted tests miss.

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

    result = asyncio.run(_run(cfg))

    reporter = Reporter()
    report_path = reporter.generate(result, cfg.output_dir)
    console.print(f"\n[bold green]Report saved:[/] {report_path}")


async def _run(cfg: Config):
    explorer = Explorer(cfg)
    return await explorer.run()
