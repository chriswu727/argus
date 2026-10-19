from __future__ import annotations

import inspect
from functools import wraps

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from argus.application.manager import SessionManager
from argus.application.operations import OPERATIONS
from argus.domain.contracts import ImageArtifact

from .catalog import (
    _CLOSED_WORLD_TOOL_NAMES,
    _CORE_TOOL_NAMES,
    _DESTRUCTIVE_TOOL_NAMES,
    _READ_ONLY_TOOL_NAMES,
    _SCREEN_TOOL_NAMES,
    _TOOL_TITLES,
)

INSTRUCTIONS = (
    "Start with start_session and follow the session protocol it returns. "
    "Exercise real user journeys, verify text-checkable findings before record_bug, "
    "and never exceed the user's authority."
)


def bind_operation(runtime: SessionManager, operation, *, raise_errors=False):
    @wraps(operation)
    async def call(*args, **kwargs):
        result = await runtime.execute(operation.__name__, operation, *args, **kwargs)
        if result.error and raise_errors:
            raise ToolError(f"[{result.error.value}] {result.value}")
        value = result.value
        if isinstance(value, list):
            return [
                Image(path=item.path) if isinstance(item, ImageArtifact) else item for item in value
            ]
        return value

    call.__signature__ = inspect.signature(operation, eval_str=True)
    return call


def tool_names(profile: str) -> set[str]:
    if profile == "core":
        return set(_CORE_TOOL_NAMES)
    if profile == "screen":
        return set(_SCREEN_TOOL_NAMES)
    if profile == "full":
        return set(OPERATIONS)
    raise ValueError("tool profile must be core, screen, or full")


def create_server(runtime: SessionManager, profile: str = "core") -> FastMCP:
    server = FastMCP("argus", instructions=INSTRUCTIONS)
    for name in sorted(tool_names(profile)):
        read_only = name in _READ_ONLY_TOOL_NAMES
        server.add_tool(
            bind_operation(runtime, OPERATIONS[name], raise_errors=True),
            name=name,
            title=_TOOL_TITLES[name],
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=False if read_only else name in _DESTRUCTIVE_TOOL_NAMES,
                idempotentHint=read_only,
                openWorldHint=name not in _CLOSED_WORLD_TOOL_NAMES,
            ),
            structured_output=False if name in {"screenshot", "screenshot_diff"} else None,
        )
    return server
