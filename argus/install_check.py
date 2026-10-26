"""No-browser, no-model smoke check of the installed stdio entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from importlib.metadata import version

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from argus import __version__
from argus.entrypoints.mcp.results import AgentResult
from argus.entrypoints.mcp.server import tool_names


async def check() -> dict:
    installed = version("argus-testing")
    if installed != __version__:
        raise RuntimeError(
            f"Installed metadata {installed} differs from source {__version__}; reinstall the package."
        )
    profiles = {}
    for profile, expected_count in (("core", 30), ("agent", 19)):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "argus.mcp_server", "--tool-profile", profile]
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                if (
                    initialized.serverInfo.name != "argus"
                    or initialized.serverInfo.version != installed
                ):
                    raise RuntimeError(
                        "MCP handshake version differs from the installed distribution"
                    )
                tools = (await client.list_tools()).tools
                if (
                    {tool.name for tool in tools} != tool_names(profile)
                    or len(tools) != expected_count
                    or not all(tool.outputSchema for tool in tools)
                ):
                    raise RuntimeError(f"Unexpected {profile} tool discovery")
                result = await client.call_tool("observe", {})
                if (
                    not result.isError
                    or not result.structuredContent
                    or result.structuredContent.get("error_code") != "no_session"
                ):
                    raise RuntimeError("Structured error contract failed")
                AgentResult.model_validate(result.structuredContent)
                profiles[profile] = len(tools)
    return {
        "version": installed,
        "profiles": profiles,
        "handshake": "passed",
        "structured_error": "passed",
    }


def main():
    print(json.dumps(asyncio.run(check()), indent=2))


if __name__ == "__main__":
    main()
