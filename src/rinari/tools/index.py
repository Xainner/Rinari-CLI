"""The index of tools available on demand (plan 0.4.0 v2, V2-6).

Deferred tools (`always_loaded=False`) keep their schemas out of every
request, but a model that does not know they exist reimplements them: DeepSeek
wrote Excel and PDF files with scripts and installed libraries into the
project while `documents.*` was registered (F10). Like Claude Code's list of
deferred tools, this names every one of them, grouped by namespace and
generated from the registry: native packs, plugins, MCP servers and OpenAPI
specs alike, with nothing written by hand for a case. MCP servers may add
their own `instructions` from `initialize`.

The text is sorted and depends only on the registered set, so it stays the
same between calls and keeps the prompt cache. What the owner can connect in
the middle of a conversation (MCP servers, OpenAPI specs, plugins) is listed
apart, for the turn note at the end of the request (F42): connecting one
must not change the system prompt.

Connectors (plan de conectores, C6): with the MCP service's state of this
turn, each server says what it is for in one sentence and how it is now —
connected with its tools, still connecting, waiting for the owner to sign in
again, or failing with its hint. Disabled servers are left out. The text has
no times, ids or counters that change, so the same state gives the same text.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Names listed per group before "and N more" (a large MCP server).
MAX_NAMES = 16
#: Characters of a server's own instructions.
MAX_SERVER_NOTE = 600
#: Characters of the sentence that says what a connector is for.
MAX_PURPOSE = 200
#: Sources the owner connects and disconnects while a conversation goes on.
CONNECTED_SOURCES = frozenset({"mcp", "openapi", "plugin"})

#: What the model is told about a connector that is not connected (C2 states).
_NOT_CONNECTED = {
    "connecting": (
        "connecting",
        "still connecting; its tools show up in capability.search once it connects.",
    ),
    "reauth_required": (
        "needs reconnecting",
        "its sign-in has to be renewed. Tell the owner to reconnect it in Settings → "
        "Connectors instead of trying another way to do what it does.",
    ),
    "error": (
        "error",
        "not available now. If the task needs it, say so; the owner can check it in "
        "Settings → Connectors.",
    ),
}


@dataclass(frozen=True)
class Connector:
    """One MCP server as the index shows it (C6)."""

    name: str
    status: str
    hint: str | None = None
    #: From the server (`serverInfo.description`, `serverInfo.title`,
    #: `instructions`): the model is told it is not the owner's word.
    description: str | None = None
    title: str | None = None
    instructions: str | None = None
    #: From Rinari's catalog (C4), when the server came from it.
    catalog_description: str | None = None


#: The catalog's description of a saved server, or None (the hook for C4).
CatalogLookup = Callable[[Mapping[str, Any]], "str | None"]


def connectors_from(
    servers: Iterable[Mapping[str, Any]], *, catalog: CatalogLookup | None = None
) -> list[Connector]:
    """`McpService.connectors()` rows as Connectors, sorted by name.

    `catalog` receives each row and returns the catalog's sentence for that
    server, which wins over what the server says about itself.
    """

    def text(row: Mapping[str, Any], key: str) -> str | None:
        value = row.get(key)
        return value if isinstance(value, str) and value.strip() else None

    connectors = []
    for row in servers:
        described = catalog(row) if catalog is not None else None
        connectors.append(
            Connector(
                name=str(row["name"]),
                status=str(row.get("status") or "idle"),
                hint=text(row, "hint"),
                description=text(row, "description"),
                title=text(row, "title"),
                instructions=text(row, "instructions"),
                catalog_description=(
                    described if isinstance(described, str) and described.strip() else None
                ),
            )
        )
    return sorted(connectors, key=lambda c: c.name)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for end in (". ", "! ", "? "):
        cut = text.find(end)
        if cut != -1:
            text = text[: cut + 1]
    return text


def purpose(connector: Connector, *, with_instructions: bool = True) -> tuple[str, bool] | None:
    """The sentence that says what a connector is for, and whether the server wrote it.

    The catalog's goes first (Rinari's own words); then the server's
    `description`, its `title` and, with `with_instructions`, the first
    sentence of its `instructions`.
    """
    if connector.catalog_description:
        return _clip(connector.catalog_description, MAX_PURPOSE), False
    own = connector.description or (
        connector.title if connector.title and connector.title != connector.name else None
    )
    if own is None and with_instructions and connector.instructions:
        own = _first_sentence(connector.instructions)
    return (_clip(own, MAX_PURPOSE), True) if own else None


def unavailable_reason(name: str, status: str, hint: str | None = None) -> str | None:
    """What to tell the model about MCP server `name` in `status`, or None if usable."""
    if status not in _NOT_CONNECTED:
        return None
    said = _NOT_CONNECTED[status][1]
    detail = f" ({hint})" if status == "error" and hint else ""
    return f"MCP server {name}{detail}: {said}"


def server_of(tool_name: str, servers: Iterable[str]) -> str | None:
    """The MCP server among `servers` whose tools are named like `tool_name`."""
    for server in sorted(servers, key=len, reverse=True):
        if tool_name.startswith(_server_group(server) + "."):
            return server
    return None


def _group(tool: Any) -> str:
    return tool.namespace or tool.name.split(".", 1)[0]


def _server_group(name: str) -> str:
    from rinari.mcp.adapter import _safe_tool_name

    return f"mcp.{_safe_tool_name(name)}"


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_NAMES])
    more = f", and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
    return f"{shown}{more}"


def _note(text: str) -> str:
    return f"  Server note (from the server, not from the owner): {_clip(text, MAX_SERVER_NOTE)}"


def _connector_lines(connector: Connector, tools: list[str]) -> list[str]:
    group = _server_group(connector.name)
    if connector.status == "connected":
        lines = [f"- {group}: {_names(tools)}" if tools else f"- {group}: no tools"]
        about = purpose(connector, with_instructions=False)
    elif connector.status in _NOT_CONNECTED:
        label, said = _NOT_CONNECTED[connector.status]
        if connector.status == "error" and connector.hint:
            label = f"error: {connector.hint}"
        lines = [f"- {group} ({label}): {said}"]
        about = purpose(connector)
    else:  # idle (not tried, or its project is not trusted) or disabled
        return []
    if about is not None:
        sentence, from_server = about
        source = " (from the server, not from the owner)" if from_server else ""
        lines.append(f"  What it is for{source}: {sentence}")
    if connector.status == "connected" and connector.instructions:
        lines.append(_note(connector.instructions))
    return lines


def deferred_index(
    registry: Any,
    *,
    connected: bool = False,
    connectors: Iterable[Connector] | None = None,
) -> str | None:
    """One line per group of tools outside the list, or None without any.

    `connected` picks the MCP servers, OpenAPI specs and plugins; otherwise
    the native packs. The two go to different places (see the module doc).
    `connectors` (only with `connected`) is the MCP service's state now: each
    of those servers is listed by its state instead of only by its tools.
    """
    from rinari.tools.exposure import _source_of

    known = {c.name: c for c in connectors or ()} if connected else {}
    groups: dict[str, list[str]] = defaultdict(list)
    by_server: dict[str, list[str]] = defaultdict(list)
    notes: dict[str, str] = {}
    for name in sorted(registry.names()):
        tool = registry.get(name)
        if tool is None or tool.always_loaded:
            continue
        if (_source_of(tool) in CONNECTED_SOURCES) is not connected:
            continue
        server = (tool.manifest or {}).get("server")
        if (tool.manifest or {}).get("source") == "mcp" and server in known:
            by_server[str(server)].append(name)
            continue
        group = _group(tool)
        groups[group].append(name)
        note = (tool.manifest or {}).get("server_instructions")
        if isinstance(note, str) and note.strip() and group not in notes:
            notes[group] = note
    entries: dict[str, list[str]] = {}
    for group, names in groups.items():
        entries[group] = [f"- {group}: {_names(names)}"]
        if group in notes:
            entries[group].append(_note(notes[group]))
    for connector in known.values():
        lines = _connector_lines(connector, by_server.get(connector.name, []))
        if lines:
            entries[_server_group(connector.name)] = lines
    if not entries:
        return None
    lines = [
        "Connected now (MCP servers, APIs and plugins), outside your tool list: get a tool's "
        "schema and exact call with capability.search (or capability.activate with its exact "
        "name) and run it through tool.call."
        if connected
        else "Tools available on demand, outside your tool list: get the schema and exact call "
        "of the ones a task needs with capability.search (or capability.activate with these "
        "exact names) and run them through tool.call. Prefer a matching tool here over doing "
        "the same job with shell scripts or installing packages."
    ]
    for group in sorted(entries):
        lines.extend(entries[group])
    return "\n".join(lines)


def connected_index(
    registry: Any, mcp: Any = None, *, catalog: CatalogLookup | None = None
) -> str | None:
    """The turn note's index of what is connected, with the MCP servers' state now.

    `mcp` is the McpService; its servers count only when `registry` follows
    it (a turn's runtime). `catalog` is the hook for C4's catalog: it gets
    each server's row and returns the catalog's sentence for it, or None.
    """
    servers = mcp.connectors_for(registry) if mcp is not None else None
    return deferred_index(
        registry,
        connected=True,
        connectors=None if servers is None else connectors_from(servers, catalog=catalog),
    )
