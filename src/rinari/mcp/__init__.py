"""MCP (Model Context Protocol) client + runtime (phase 5).

Decision record (2026-08-17, see TODO.md "Registro de decisiones"):

- v1 transport: **stdio** (newline-delimited JSON-RPC 2.0 over the
  subprocess's stdin/stdout), the primary MCP transport. The transport layer
  is an abstraction so an HTTP transport can be added without touching the
  client or adapter.
- MCP servers are *external capability providers*, never privileged side
  channels. Their tools are normalized into `ToolDefinition`s and pass
  through the normal Policy Engine (mcp.read / mcp.call), approvals,
  schema validation, redaction, tracing and cancellation (harness.md 109,
  AGENTS.md 18).
- Namespacing: `mcp.<server>.<tool>`.
- Project scope: project-local MCP definitions require project trust.
- Secrets: server config may carry env-var references, never values.
"""

from rinari.mcp.adapter import mcp_tool_definitions
from rinari.mcp.client import McpClient, McpError
from rinari.mcp.protocol import (
    METHOD_INITIALIZE,
    METHOD_PROMPTS_GET,
    METHOD_PROMPTS_LIST,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpMessage,
    parse_message,
    request,
)
from rinari.mcp.service import McpService
from rinari.mcp.transport import (
    InProcessTransport,
    McpTransport,
    StdioTransport,
)

__all__ = [
    "METHOD_INITIALIZE",
    "METHOD_PROMPTS_GET",
    "METHOD_PROMPTS_LIST",
    "METHOD_RESOURCES_LIST",
    "METHOD_RESOURCES_READ",
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "InProcessTransport",
    "McpClient",
    "McpError",
    "McpMessage",
    "McpService",
    "McpTransport",
    "StdioTransport",
    "mcp_tool_definitions",
    "parse_message",
    "request",
]
