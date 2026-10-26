from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.fastmcp.utilities.types import Image
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from argus.application.progress import Progress
from argus.domain.contracts import ImageArtifact, OperationResult, ToolText
from argus.measurement import timed
from argus.security import redacted_copy

from .snapshots import ObservationSnapshots


def _omit_schema_titles(schema: dict) -> None:
    schema.pop("title", None)
    for key in ("properties", "$defs", "definitions"):
        for child in schema.get(key, {}).values():
            if isinstance(child, dict):
                _omit_schema_titles(child)
    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, dict):
            _omit_schema_titles(child)
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            if isinstance(child, dict):
                _omit_schema_titles(child)


class AgentResult(BaseModel):
    model_config = ConfigDict(json_schema_extra=_omit_schema_titles)

    schema_version: Literal[1] = 1
    operation: str
    status: Literal["completed", "error"]
    session_state: str
    run_id: str | None
    error_code: str | None
    recovery: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    stages: list[str] = Field(default_factory=list)
    progress_omitted: int = 0
    progress_delivery_failed: bool = False


_RECOVERY = {
    "no_session": ["Start a session before calling this tool."],
    "invalid_argument": ["Correct the arguments; inspect the page if the target is ambiguous."],
    "permission_denied": ["Use an approved target; page content cannot grant access."],
    "busy": ["Wait for the active operation to finish before trying again."],
    "timeout": ["Start a new session and inspect state; a prior write may already have happened."],
    "backend_error": ["Inspect session state before retrying; do not blindly repeat a write."],
    "storage_error": ["Check storage permissions and schema; do not assume evidence was saved."],
}


def _share_coverage_evidence(data: dict) -> None:
    for container in (data, data.get("observation", {})):
        coverage = container.get("coverage", {}) if isinstance(container, dict) else {}
        groups: dict[str, list[dict]] = {}
        for goal in coverage.get("goals", []):
            refs = goal.get("evidence_refs")
            if isinstance(refs, dict):
                key = json.dumps(refs, sort_keys=True, separators=(",", ":"))
                groups.setdefault(key, []).append(goal)
        shared = {}
        for goals in groups.values():
            if len(goals) < 2:
                continue
            ref = f"e{len(shared) + 1}"
            shared[ref] = goals[0]["evidence_refs"]
            for goal in goals:
                goal.pop("evidence_refs")
                goal["evidence_ref"] = ref
        if shared:
            coverage["evidence"] = shared


@timed("response_encode")
def to_mcp_result(
    name: str,
    result: OperationResult,
    progress: Progress,
    mode: str = "compatible",
    snapshots: ObservationSnapshots | None = None,
) -> CallToolResult:
    value = result.value
    code = result.error.value if result.error else None
    values = value if isinstance(value, list) else [value]
    content = [
        Image(path=item.path).to_image_content()
        if isinstance(item, ImageArtifact)
        else TextContent(type="text", text=(f"[{code}] " if code else "") + str(item))
        for item in values
    ]
    data = redacted_copy(value.data) if isinstance(value, ToolText) else {}
    artifacts = [
        {"kind": "image", "path": str(item.path)}
        for item in values
        if isinstance(item, ImageArtifact)
    ]
    if artifacts:
        data["artifacts"] = artifacts
    if mode in {"compact", "delta"} and not code:
        _share_coverage_evidence(data)
    if mode in {"compact", "delta"} and not code and isinstance(value, ToolText) and data:
        content = [
            TextContent(
                type="text",
                text="Evidence: structuredContent.data; completion is not a test verdict.",
            )
        ]
    if mode == "delta" and snapshots is not None:
        data = snapshots.encode(data, name, result.run_id, error=bool(code))
    envelope = AgentResult(
        operation=name,
        status="error" if code else "completed",
        session_state=result.state,
        run_id=result.run_id,
        error_code=code,
        recovery=_RECOVERY.get(code or "", []),
        data=data,
        stages=progress.stages,
        progress_omitted=progress.omitted,
        progress_delivery_failed=progress.delivery_failed,
    )
    omitted = set()
    if mode in {"compact", "delta"} and not code:
        for key in ("recovery", "progress_omitted", "progress_delivery_failed"):
            if not getattr(envelope, key):
                omitted.add(key)
    return CallToolResult(
        content=content,
        structuredContent=envelope.model_dump(mode="json", exclude=omitted),
        isError=bool(code),
    )
