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
same between calls and keeps the prompt cache.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

#: Names listed per group before "and N more" (a large MCP server).
MAX_NAMES = 16
#: Characters of a server's own instructions.
MAX_SERVER_NOTE = 600


def _group(tool: Any) -> str:
    return tool.namespace or tool.name.split(".", 1)[0]


def deferred_index(registry: Any) -> str | None:
    """One line per group of tools that load on demand, or None without any."""
    groups: dict[str, list[str]] = defaultdict(list)
    notes: dict[str, str] = {}
    for name in sorted(registry.names()):
        tool = registry.get(name)
        if tool is None or tool.always_loaded:
            continue
        group = _group(tool)
        groups[group].append(name)
        note = (tool.manifest or {}).get("server_instructions")
        if isinstance(note, str) and note.strip() and group not in notes:
            text = " ".join(note.split())
            notes[group] = (
                text[: MAX_SERVER_NOTE - 1] + "…" if len(text) > MAX_SERVER_NOTE else text
            )
    if not groups:
        return None
    lines = [
        "Tools available on demand (registered, not loaded yet). Load the ones a task needs "
        "with capability.activate using these exact names (or find them with "
        "capability.search), then call them. Prefer a matching tool here over doing the "
        "same job with shell scripts or installing packages."
    ]
    for group in sorted(groups):
        names = groups[group]
        shown = ", ".join(names[:MAX_NAMES])
        more = f", and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
        lines.append(f"- {group}: {shown}{more}")
        if group in notes:
            lines.append(f"  Server note (from the server, not from the owner): {notes[group]}")
    return "\n".join(lines)
