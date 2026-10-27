"""Names that differ between MCP Python SDK 1.x and 2.x.

Argus pins 1.x. 2.x renames FastMCP to MCPServer, accepts the product version
in its constructor, and exposes protocol model fields in snake_case. Reading
every version-sensitive name through this module keeps a future switch to a
dependency change; a non-blocking CI job runs Argus against 2.x.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

try:  # mcp 1.x
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.fastmcp.utilities.types import Image
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import Context, MCPServer  # type: ignore[no-redef]
    from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[no-redef]
    from mcp.server.mcpserver.utilities.types import Image  # type: ignore[no-redef]

__all__ = ["Context", "Image", "MCPServer", "ToolError", "create_mcp_server", "field"]

_ACCEPTS_VERSION = "version" in inspect.signature(MCPServer.__init__).parameters


def create_mcp_server(name: str, instructions: str, version: str) -> MCPServer:
    if _ACCEPTS_VERSION:
        return MCPServer(name, instructions=instructions, version=version)
    server = MCPServer(name, instructions=instructions)
    # FastMCP 1.x does not expose the low-level server's product version argument.
    server._mcp_server.version = version
    return server


def field(model: Any, name: str) -> Any:
    """Read a protocol model field by its wire (camelCase) name on either SDK."""
    if hasattr(model, name):
        return getattr(model, name)
    return getattr(model, re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())
