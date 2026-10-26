from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp.utilities.types import Image
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from argus.application.progress import Progress
from argus.domain.contracts import ImageArtifact, OperationResult, ToolText
from argus.security import redacted_copy


class AgentResult(BaseModel):
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


def to_mcp_result(name: str, result: OperationResult, progress: Progress) -> CallToolResult:
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
    return CallToolResult(
        content=content, structuredContent=envelope.model_dump(mode="json"), isError=bool(code)
    )
