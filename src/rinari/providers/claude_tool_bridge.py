"""The MCP server Claude Code sees as Rinari's tools (Claude Subscription, phase D).

Claude Code only hands the model tools it knows about, and with its built-in
tools off the only way to show it Rinari's is an MCP server. This is that
server, and it is deliberately empty-handed: it answers `tools/list` with the
schemas Rinari wrote for this call and never executes anything.

The call itself goes back through Rinari. The adapter reads the model's
`tool_use` from the stream and stops the child as soon as the message ends
with `stop_reason: tool_use`; Rinari then runs the tool in its own loop, with
its policy, approvals, budgets and activity, like with any other provider.
If Claude Code asks this server to run a tool before the child is stopped,
the request waits here until the process is killed: nothing runs twice and
no second generation starts.

Started by Claude Code as `python -m rinari.providers.claude_tool_bridge
<tools.json>`. It imports nothing from Rinari beyond the standard library, so
it starts in a fraction of the time a full engine import would take.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "rinari"
#: Claude Code exposes an MCP tool as `mcp__<server>__<tool>`.
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
_DEFAULT_PROTOCOL = "2025-06-18"


def _reply(message_id: Any, *, result: Any = None, error: dict | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(tools: list[dict[str, Any]], stdin: Any = None) -> None:
    """Answer MCP requests on stdin until it closes."""
    stdin = stdin if stdin is not None else sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # notifications need no answer
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") or {}
            _reply(
                message["id"],
                result={
                    "protocolVersion": params.get("protocolVersion") or _DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "1"},
                },
            )
        elif method == "tools/list":
            _reply(message["id"], result={"tools": tools})
        elif method == "ping":
            _reply(message["id"], result={})
        elif method == "tools/call":
            # Rinari runs the tool, not this process. No answer is sent: the
            # adapter stops the child at the end of the message, and until
            # then Claude Code waits instead of running anything itself.
            continue
        else:
            _reply(message["id"], error={"code": -32601, "message": f"unknown method {method}"})


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m rinari.providers.claude_tool_bridge <tools.json>\n")
        return 2
    tools = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    serve(tools if isinstance(tools, list) else [])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
