from __future__ import annotations

import json

from mcp.types import CallToolResult, Tool

from .compat import field


def json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def response_bytes(result: CallToolResult) -> dict[str, int]:
    return {
        "result_json_bytes": len(result.model_dump_json(by_alias=True, exclude_none=True).encode()),
        "text_utf8_bytes": sum(
            len(block.text.encode()) for block in result.content if block.type == "text"
        ),
        "structured_json_bytes": json_bytes(field(result, "structuredContent"))
        if field(result, "structuredContent") is not None
        else 0,
        "image_base64_bytes": sum(
            len(block.data.encode()) for block in result.content if block.type == "image"
        ),
        "images": sum(block.type == "image" for block in result.content),
    }


def discovery_bytes(tools: list[Tool], instructions: str = "") -> dict[str, int]:
    return {
        "tools": len(tools),
        "tools_json_bytes": json_bytes(
            [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        ),
        "instructions_utf8_bytes": len(instructions.encode()),
        "description_utf8_bytes": sum(len((tool.description or "").encode()) for tool in tools),
        "input_schema_json_bytes": sum(json_bytes(field(tool, "inputSchema")) for tool in tools),
        "output_schema_json_bytes": sum(
            json_bytes(field(tool, "outputSchema")) for tool in tools if field(tool, "outputSchema")
        ),
    }
