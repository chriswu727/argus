from __future__ import annotations

import inspect
from functools import wraps

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from argus.application.manager import SessionManager
from argus.application.operations import OPERATIONS
from argus.application.progress import progress_scope
from argus.domain.contracts import ImageArtifact

from .catalog import (
    _AGENT_TOOL_DESCRIPTIONS,
    _CLOSED_WORLD_TOOL_NAMES,
    _CORE_TOOL_NAMES,
    _DESTRUCTIVE_TOOL_NAMES,
    _READ_ONLY_TOOL_NAMES,
    _SCREEN_TOOL_NAMES,
    _TOOL_TITLES,
)
from .results import AgentResult, to_mcp_result

INSTRUCTIONS = (
    "Start with start_session; follow its protocol. Exercise real journeys and "
    "verify findings before record_bug. Read structured data: completed means "
    "the tool ran, not the application passed. Never exceed the user's authority."
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
    if profile == "agent":
        return set(_AGENT_TOOL_DESCRIPTIONS)
    if profile == "core":
        return set(_CORE_TOOL_NAMES)
    if profile == "screen":
        return set(_SCREEN_TOOL_NAMES)
    if profile == "full":
        return set(OPERATIONS)
    raise ValueError("tool profile must be agent, core, screen, or full")


def bind_tool(runtime: SessionManager, operation):
    @wraps(operation)
    async def call(*args, ctx: Context | None = None, **kwargs):
        async def notify(sequence: int, message: str) -> None:
            if ctx is not None:
                await ctx.report_progress(sequence, message=message)

        with progress_scope(notify if ctx is not None else None) as progress:
            result = await runtime.execute(operation.__name__, operation, *args, **kwargs)
        return to_mcp_result(operation.__name__, result, progress, runtime.settings.response_mode)

    signature = inspect.signature(operation, eval_str=True)
    parameters = [
        *signature.parameters.values(),
        inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None
        ),
    ]
    call.__signature__ = signature.replace(parameters=parameters, return_annotation=AgentResult)
    call.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in parameters},
        "return": AgentResult,
    }
    return call


def create_server(runtime: SessionManager, profile: str = "core") -> FastMCP:
    server = FastMCP("argus", instructions=INSTRUCTIONS)
    for name in sorted(tool_names(profile)):
        read_only = name in _READ_ONLY_TOOL_NAMES
        server.add_tool(
            bind_tool(runtime, OPERATIONS[name]),
            name=name,
            description=_AGENT_TOOL_DESCRIPTIONS[name] if profile == "agent" else None,
            title=_TOOL_TITLES[name],
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=False if read_only else name in _DESTRUCTIVE_TOOL_NAMES,
                idempotentHint=read_only,
                openWorldHint=name not in _CLOSED_WORLD_TOOL_NAMES,
            ),
            structured_output=True,
        )
    return server
