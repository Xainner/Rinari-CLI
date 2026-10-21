"""What the policy knows about one MCP tool call (plan de conectores, C5).

An MCP server describes its tools with annotations (`readOnlyHint`,
`destructiveHint`, `openWorldHint`). They are hints from the server, not
guarantees: the owner can correct them per tool, and can set Allow / Ask /
Block per tool or as a default for every tool of a server. The adapter turns
all of that into `McpToolFacts`, which the policy reads with the mode.

Only an explicit `true`/`false` counts as an annotation. The MCP spec's
defaults (`destructiveHint` and `openWorldHint` true when absent) are not
applied to the mode rules: a tool without annotations keeps the behavior of
permissions v4 (a registered server is a trusted destination). The guard of
external content does read `openWorldHint` as the spec says: a tool that does
not declare `openWorldHint: false` may send data outside.
"""

from __future__ import annotations

from dataclasses import dataclass

DECISION_ALLOW = "allow"
DECISION_ASK = "ask"
DECISION_DENY = "deny"
DECISIONS = (DECISION_ALLOW, DECISION_ASK, DECISION_DENY)

#: Where the effective decision of a tool comes from.
SOURCE_TOOL = "tool"  # the owner's setting for this tool
SOURCE_SERVER = "server"  # the owner's default for the server
SOURCE_MODE = "mode"  # no setting: the mode's rule decides

#: Annotations the owner can correct, by their MCP name.
READ_ONLY_HINT = "readOnlyHint"
DESTRUCTIVE_HINT = "destructiveHint"
OPEN_WORLD_HINT = "openWorldHint"
ANNOTATION_KEYS = (READ_ONLY_HINT, DESTRUCTIVE_HINT, OPEN_WORLD_HINT)

#: Policy rules of MCP tools whose «Siempre» saves the tool's setting.
RULE_TOOL_ASK = "mcp_tool_ask"
RULE_DESTRUCTIVE = "mcp_destructive"
RULE_BLOCKED = "mcp_tool_blocked"
LASTING_TOOL_RULES = frozenset({RULE_TOOL_ASK, RULE_DESTRUCTIVE})


@dataclass(frozen=True, slots=True)
class McpToolFacts:
    """One MCP tool, as the policy judges it."""

    server: str
    tool: str  # the server's own name for the tool
    read_only: bool = False
    destructive: bool = False
    # None: the server did not say (the guard reads it as "may send out").
    open_world: bool | None = None
    decision: str | None = None  # allow | ask | deny | None (the mode decides)
    decision_source: str = SOURCE_MODE

    @property
    def origin(self) -> str:
        """The external-content origin of this server's results."""
        return mcp_origin(self.server)

    @property
    def sends_out(self) -> bool:
        """A call can carry data to the outside world (guard of external content)."""
        return not self.read_only and self.open_world is not False


def mcp_origin(server: str) -> str:
    return f"mcp:{server}"


def effective_annotations(
    server_annotations: dict | None, corrections: dict | None = None
) -> dict[str, bool | None]:
    """The hints that count: the owner's corrections over the server's.

    «This one is destructive» also means it is not a read, unless the owner
    said both: a destructive hint means nothing on a read (the spec).
    """
    corrections = corrections or {}
    merged: dict[str, bool | None] = {}
    for key in ANNOTATION_KEYS:
        value = corrections.get(key)
        if not isinstance(value, bool):
            value = (server_annotations or {}).get(key)
        merged[key] = value if isinstance(value, bool) else None
    if corrections.get(DESTRUCTIVE_HINT) is True and READ_ONLY_HINT not in corrections:
        merged[READ_ONLY_HINT] = False
    return merged


def facts_for(
    server: str,
    tool: str,
    annotations: dict[str, bool | None],
    *,
    decision: str | None = None,
    decision_source: str = SOURCE_MODE,
) -> McpToolFacts:
    read_only = annotations.get(READ_ONLY_HINT) is True
    return McpToolFacts(
        server=server,
        tool=tool,
        read_only=read_only,
        # The spec: destructiveHint only means something for a tool that writes.
        destructive=not read_only and annotations.get(DESTRUCTIVE_HINT) is True,
        open_world=annotations.get(OPEN_WORLD_HINT),
        decision=decision if decision in DECISIONS else None,
        decision_source=decision_source if decision in DECISIONS else SOURCE_MODE,
    )


__all__ = [
    "ANNOTATION_KEYS",
    "DECISIONS",
    "DECISION_ALLOW",
    "DECISION_ASK",
    "DECISION_DENY",
    "DESTRUCTIVE_HINT",
    "LASTING_TOOL_RULES",
    "OPEN_WORLD_HINT",
    "READ_ONLY_HINT",
    "RULE_BLOCKED",
    "RULE_DESTRUCTIVE",
    "RULE_TOOL_ASK",
    "SOURCE_MODE",
    "SOURCE_SERVER",
    "SOURCE_TOOL",
    "McpToolFacts",
    "effective_annotations",
    "facts_for",
    "mcp_origin",
]
