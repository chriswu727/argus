from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent

from argus.access import AccessPolicy
from argus.bootstrap import create_runtime
from argus.entrypoints.mcp.overhead import discovery_bytes, response_bytes
from argus.entrypoints.mcp.server import create_server
from argus.security import redacted_copy
from argus.settings import Settings

from .browser_baseline import BrowserBaseline


class CallBudget:
    def __init__(self, limit: int):
        if not 1 <= limit <= 60:
            raise ValueError("tool budget must be between 1 and 60")
        self.limit = limit
        self.used = 0

    def consume(self) -> int:
        if self.used >= self.limit:
            raise RuntimeError("Tool budget exhausted; finish using existing evidence.")
        self.used += 1
        return self.used


async def serve(arm: str, origin: str, output: Path, limit: int = 60):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline = BrowserBaseline(origin)
    runtime = create_runtime(
        Settings(
            output_dir=output / "reports",
            response_mode="compact",
            access=AccessPolicy(allowed_origins=(origin,)),
        )
    )
    inner = create_server(runtime, "web") if arm == "argus" else baseline.server()
    outer = Server("testing", instructions=inner.instructions)
    budget = CallBudget(limit)
    lock = asyncio.Lock()

    @outer.list_tools()
    async def list_tools():
        tools = await inner.list_tools()
        (output / "discovery.json").write_text(
            json.dumps(discovery_bytes(tools, inner.instructions or ""))
        )
        return tools

    @outer.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        async with lock:
            sequence = budget.consume()
            started = time.monotonic()
            try:
                result = await inner.call_tool(name, arguments or {})
                if isinstance(result, tuple):
                    result = CallToolResult(content=result[0], structuredContent=result[1])
                elif not isinstance(result, CallToolResult):
                    result = CallToolResult(content=result)
            except Exception as exc:
                result = CallToolResult(
                    content=[TextContent(type="text", text=f"{type(exc).__name__}: {exc}")],
                    isError=True,
                )
            entry = {
                "call": sequence,
                "tool": name,
                "arguments": redacted_copy(arguments),
                "seconds": time.monotonic() - started,
                "is_error": result.isError,
                "data": result.structuredContent,
                "text": [block.text[:20000] for block in result.content if block.type == "text"],
                "images": sum(block.type == "image" for block in result.content),
                "response_bytes": response_bytes(result),
                "runtime_overhead": runtime.snapshot()["overhead"] if arm == "argus" else None,
            }
            with (output / "tools.jsonl").open("a") as trace:
                trace.write(json.dumps(entry) + "\n")
            return result

    try:
        async with stdio_server() as (read, write):
            await outer.run(read, write, outer.create_initialization_options())
    finally:
        await runtime.shutdown()
        await baseline.close()


def main():
    parser = argparse.ArgumentParser(description="Isolated evaluation MCP gateway")
    parser.add_argument("--arm", choices=("baseline", "argus"), required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(serve(args.arm, args.origin, args.output, args.limit))


if __name__ == "__main__":
    main()
