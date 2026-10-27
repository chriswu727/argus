from __future__ import annotations

import inspect
from functools import wraps

from mcp.types import ToolAnnotations

from argus import __version__
from argus.application.manager import SessionManager
from argus.application.operations import OPERATIONS
from argus.application.progress import progress_scope
from argus.domain.contracts import ImageArtifact
from argus.measurement import measurement_scope

from .catalog import (
    _CLOSED_WORLD_TOOL_NAMES,
    _CONCISE_TOOL_DESCRIPTIONS,
    _DESTRUCTIVE_TOOL_NAMES,
    _READ_ONLY_TOOL_NAMES,
    _SCREEN_TOOL_NAMES,
    _TOOL_TITLES,
    _WEB_TOOL_NAMES,
    PROFILE_ALIASES,
    PROFILES,
)
from .compat import Context, Image, MCPServer, ToolError, create_mcp_server
from .overhead import response_bytes
from .results import AgentResult, _omit_schema_titles, to_mcp_result
from .snapshots import FIELDS, ObservationSnapshots

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


def resolve_profile(profile: str) -> str:
    resolved = PROFILE_ALIASES.get(profile, profile)
    if resolved not in PROFILES:
        raise ValueError("tool profile must be web, advanced, screen or full")
    return resolved


def tool_names(profile: str) -> set[str]:
    profile = resolve_profile(profile)
    if profile == "web":
        return set(_WEB_TOOL_NAMES)
    if profile == "advanced":
        return {
            name
            for name in OPERATIONS
            if not name.startswith("screen_") and name != "start_screen_session"
        }
    if profile == "screen":
        return set(_SCREEN_TOOL_NAMES)
    return set(OPERATIONS)


def bind_tool(runtime: SessionManager, operation, snapshots: ObservationSnapshots | None = None):
    @wraps(operation)
    async def call(*args, ctx: Context | None = None, **kwargs):
        async def notify(sequence: int, message: str) -> None:
            if ctx is not None:
                await ctx.report_progress(sequence, message=message)

        with measurement_scope() as measurements:
            with progress_scope(notify if ctx is not None else None) as progress:
                try:
                    result = await runtime.execute(operation.__name__, operation, *args, **kwargs)
                except BaseException:
                    if snapshots is not None:
                        snapshots.clear()
                    raise
            encoded = to_mcp_result(
                operation.__name__, result, progress, runtime.settings.response_mode, snapshots
            )
        runtime.response_totals.update(response_bytes(encoded))
        runtime.response_totals["responses"] += 1
        runtime.component_seconds.update(measurements.seconds)
        runtime.component_calls.update(measurements.calls)
        return encoded

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


def create_server(runtime: SessionManager, profile: str = "web") -> MCPServer:
    instructions = INSTRUCTIONS
    if runtime.settings.response_mode == "delta":
        instructions += (
            " Delta mode: retain observation snapshots by ID. In delta snapshots, inherit only "
            + ", ".join(sorted(FIELDS))
            + "; replace returned fields and delete snapshot.removed fields. base_id must match. "
            "Other fields are current-call-only. On a missing base call observe for full state. "
            "Never infer unchanged from an unavailable base."
        )
    server = create_mcp_server("argus", instructions, __version__)
    snapshots = ObservationSnapshots()
    # Web profiles use concise descriptions; screen and full keep full docstrings.
    concise = resolve_profile(profile) in {"web", "advanced"}
    for name in sorted(tool_names(profile)):
        read_only = name in _READ_ONLY_TOOL_NAMES
        server.add_tool(
            bind_tool(runtime, OPERATIONS[name], snapshots),
            name=name,
            description=(_CONCISE_TOOL_DESCRIPTIONS.get(name) if concise else None)
            # Python 3.13 dedents docstrings at compile time; earlier versions do
            # not, so normalize to advertise identical descriptions everywhere.
            or inspect.cleandoc(OPERATIONS[name].__doc__ or ""),
            title=_TOOL_TITLES[name],
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=False if read_only else name in _DESTRUCTIVE_TOOL_NAMES,
                idempotentHint=read_only,
                openWorldHint=name not in _CLOSED_WORLD_TOOL_NAMES,
            ),
            structured_output=True,
        )
        # FastMCP has no public hook for advertised input schemas; degrade to the
        # SDK's schema if this private manager changes (a test pins the result).
        manager = getattr(server, "_tool_manager", None)
        registered = manager.get_tool(name) if manager is not None else None
        if registered is not None:
            _omit_schema_titles(registered.parameters)
    return server
