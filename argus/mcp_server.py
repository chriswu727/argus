"""MCP process entry point and compatibility imports for Python integrations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from argus import __version__
from argus.application.operations import OPERATIONS
from argus.bootstrap import create_runtime
from argus.entrypoints.mcp.catalog import (
    _CLOSED_WORLD_TOOL_NAMES as _CLOSED_WORLD_TOOL_NAMES,
)
from argus.entrypoints.mcp.catalog import (
    _CORE_TOOL_NAMES as _CORE_TOOL_NAMES,
)
from argus.entrypoints.mcp.catalog import (
    _DESTRUCTIVE_TOOL_NAMES as _DESTRUCTIVE_TOOL_NAMES,
)
from argus.entrypoints.mcp.catalog import (
    _READ_ONLY_TOOL_NAMES as _READ_ONLY_TOOL_NAMES,
)
from argus.entrypoints.mcp.catalog import (
    _SCREEN_TOOL_NAMES as _SCREEN_TOOL_NAMES,
)
from argus.entrypoints.mcp.catalog import (
    _TOOL_TITLES as _TOOL_TITLES,
)
from argus.entrypoints.mcp.server import bind_operation, create_server, tool_names
from argus.settings import Settings

runtime = create_runtime(Settings())
mcp = create_server(runtime, "full")


def __getattr__(name):
    if name in OPERATIONS:
        return bind_operation(runtime, OPERATIONS[name])
    from argus import _compat

    if hasattr(_compat, name):
        return getattr(_compat, name)
    if name == "Reporter":
        from argus.reporter import Reporter

        return Reporter
    raise AttributeError(name)


def _argus_version() -> str:
    return __version__


def _apply_tool_profile(profile: str) -> tuple[int, int]:
    allowed = tool_names(profile)
    for name in set(OPERATIONS) - allowed:
        mcp.remove_tool(name)
    return len(OPERATIONS), len(allowed)


async def serve(settings: Settings, profile: str) -> None:
    instance = create_runtime(settings)
    server = create_server(instance, profile)
    monitor = None
    try:
        if settings.monitor_port is not None:
            from argus.adapters.monitoring import Monitor

            monitor = Monitor(instance, settings.monitor_port)
            await monitor.start()
            print(f"Argus monitor: {monitor.url}", file=sys.stderr)
        await server.run_stdio_async()
    finally:
        await instance.shutdown()
        if monitor:
            await monitor.stop()


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Argus QA MCP server")
    parser.add_argument("--version", "-V", action="version", version=f"argus-testing {__version__}")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--unsafe", action="store_true", default=None)
    parser.add_argument(
        "--tool-profile",
        choices=("core", "screen", "full"),
        default=os.environ.get("ARGUS_TOOL_PROFILE", "core"),
    )
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument(
        "--monitor-port", type=int, help="Enable local monitoring; 0 chooses a free port"
    )
    args = parser.parse_args()
    if args.doctor:
        from argus.screen.permissions import main as doctor

        sys.exit(doctor())
    if args.list_tools:
        names = sorted(tool_names(args.tool_profile))
        print(f"Argus tool profile: {args.tool_profile} ({len(names)} tools)")
        print("\n".join(names))
        return
    try:
        settings = Settings.from_env(monitor_port=args.monitor_port, unsafe_eval=args.unsafe)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from argus.adapters.logging import configure_logging

    configure_logging()
    try:
        asyncio.run(serve(settings, args.tool_profile))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
