"""What the «Resumen» panel cannot gather by itself (plan 0.4.0 #82).

`session.summary` answers four lists the desktop has no other way to build:

- sources: the pages Rinari read and the searches it ran, plus what each MCP
  connector was asked, grouped by connector;
- results: the files it delivered or produced in the conversation;
- connectors: the enabled MCP servers with their state, and active skills;
- background: processes still running and the scheduled tasks of the
  conversation or its project.

Everything comes from the session's own events and live registries; nothing
is invented and nothing is called on the network. Environment, context,
plan, checklist, changes and agents the desktop already has.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePath
from typing import Any

PAGE_TOOLS = ("web.fetch", "web.extract_metadata", "browser.open", "browser.navigate")
SEARCH_TOOLS = ("web.search",)
RESULT_TOOLS = ("artifact.export", "web.download", "documents.render", "file.present", "fs.write")
SUMMARY_TOOLS = (*PAGE_TOOLS, *SEARCH_TOOLS, *RESULT_TOOLS)
TERMINAL = ("turn.completed", "turn.failed", "turn.stopped", "turn.cancelled")
MAX_ITEMS = 50

_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"},
    "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"},
    "document": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".odt"},
}
# fs.write is code most of the time; only these count as something delivered.
_DELIVERABLE = set().union(*_KINDS.values())


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def kind_of(path: str, mime: str | None = None) -> str:
    if mime:
        head = mime.split("/", 1)[0]
        if head in ("image", "video", "audio"):
            return head
        if "pdf" in mime or "officedocument" in mime or "msword" in mime:
            return "document"
    suffix = PurePath(path).suffix.lower()
    return next((kind for kind, exts in _KINDS.items() if suffix in exts), "file")


def _mcp_connector(tool: str) -> tuple[str, str] | None:
    if not tool.startswith("mcp."):
        return None
    _, _, rest = tool.partition(".")
    connector, _, name = rest.partition(".")
    return (connector, name) if connector and name else None


def _calls(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Requested + completed tool calls this summary reads, in order."""
    marks = ", ".join("?" for _ in SUMMARY_TOOLS)
    rows = db.query(
        "SELECT type, payload_json FROM session_events WHERE session_id = ? "
        "AND type IN ('tool.requested', 'tool.completed') "
        f"AND (json_extract(payload_json, '$.tool') IN ({marks}) "
        "OR json_extract(payload_json, '$.tool') LIKE 'mcp.%') ORDER BY seq",
        (session_id, *SUMMARY_TOOLS),
    )
    calls: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _json(row["payload_json"])
        call_id = str(payload.get("tool_call_id") or "")
        if not call_id:
            continue
        call = calls.setdefault(call_id, {"tool": payload.get("tool")})
        if row["type"] == "tool.requested":
            call["arguments"] = _json(payload.get("arguments"))
            call["turn_id"] = payload.get("turn_id")
            call["at"] = payload.get("occurred_at")
        else:
            observation = _json(payload.get("observation"))
            call["ok"] = bool(payload.get("ok", observation.get("ok")))
            call["data"] = observation.get("data") if isinstance(observation, dict) else None
    return [call for call in calls.values() if call.get("ok")]


def _sources(calls: list[dict[str, Any]]) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    searches: dict[str, dict[str, Any]] = {}
    connectors: dict[str, dict[str, Any]] = {}
    for call in calls:
        tool = str(call.get("tool") or "")
        args = call.get("arguments") or {}
        data = call.get("data") if isinstance(call.get("data"), dict) else {}
        if tool in PAGE_TOOLS:
            url = data.get("final_url") or args.get("url") or data.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                title = data.get("title") if isinstance(data.get("title"), str) else None
                pages.pop(url, None)
                pages[url] = {"url": url, "title": title, "tool": tool, "at": call.get("at")}
        elif tool in SEARCH_TOOLS:
            queries = args.get("queries") or [args.get("query")]
            for query in queries if isinstance(queries, list) else []:
                if isinstance(query, str) and query.strip():
                    searches.pop(query, None)
                    searches[query] = {"query": query, "at": call.get("at")}
        elif (mcp := _mcp_connector(tool)) is not None:
            connector, name = mcp
            entry = connectors.setdefault(
                connector, {"connector": connector, "calls": 0, "tools": []}
            )
            entry["calls"] += 1
            if name not in entry["tools"]:
                entry["tools"].append(name)
            entry["at"] = call.get("at")
    return {
        "pages": list(reversed(pages.values()))[:MAX_ITEMS],
        "searches": list(reversed(searches.values()))[:MAX_ITEMS],
        "mcp": sorted(connectors.values(), key=lambda c: str(c.get("at") or ""), reverse=True),
        "total": len(pages) + len(searches),
    }


def _results(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for call in calls:
        tool = str(call.get("tool") or "")
        args = call.get("arguments") or {}
        data = call.get("data") if isinstance(call.get("data"), dict) else {}
        path = mime = None
        if tool == "artifact.export":
            path, mime = data.get("path") or args.get("path"), data.get("mime_type")
        elif tool == "web.download":
            path, mime = data.get("path"), data.get("content_type")
        elif tool == "documents.render":
            path = args.get("document")
        elif tool == "file.present":
            path = args.get("path") or data.get("path")
        elif tool == "fs.write":
            candidate = args.get("path")
            if isinstance(candidate, str) and PurePath(candidate).suffix.lower() in _DELIVERABLE:
                path = data.get("path") or candidate
        if not isinstance(path, str) or not path:
            continue
        found.pop(path, None)
        found[path] = {
            "path": path,
            "name": PurePath(path).name,
            "kind": kind_of(path, mime if isinstance(mime, str) else None),
            "tool": tool,
            "turn_id": call.get("turn_id"),
            "at": call.get("at"),
            "exists": Path(path).exists(),
        }
    return list(reversed(found.values()))[:MAX_ITEMS]


def _connectors(server: Any, record: Any) -> dict[str, Any]:
    servers = []
    for row in server._services.mcp.list(None):
        if not row.get("enabled"):
            continue
        try:
            connected = bool(server._mcp_connected(row["name"]))
            status = server._services.mcp.status(row)["status"]
        except Exception:
            connected, status = False, None
        servers.append(
            {
                "name": row.get("name"),
                "scope": row.get("scope") or "global",
                "connected": connected,
                # The connection state (C2): connecting, error, reauth_required…
                "status": status,
            }
        )
    skills = [{"name": name, "version": version} for name, version in (record.active_skills or ())]
    return {"mcp": servers, "skills": skills}


def _background(server: Any, record: Any) -> dict[str, Any]:
    processes = []
    try:
        listed = server._process_tasks.list({"session_id": record.id, "limit": 100})
        for row in listed.get("processes", []):
            if row.get("running"):
                processes.append(
                    {
                        key: row.get(key)
                        for key in ("id", "kind", "command", "cwd", "started_at", "readiness")
                    }
                )
    except Exception:
        processes = []
    scheduled = []
    try:
        for task in server._schedule.tasks.list():
            mine = (task.get("last_run") or {}).get("session_id") == record.id or (
                record.project_id and task.get("project_id") == record.project_id
            )
            if mine:
                scheduled.append(
                    {
                        key: task.get(key)
                        for key in ("id", "name", "kind", "enabled", "description", "next_run_at")
                    }
                )
    except Exception:
        scheduled = []
    return {"processes": processes, "scheduled": scheduled}


def build_summary(server: Any, session_id: str) -> dict[str, Any]:
    record = server._services.sessions.show(session_id)
    calls = _calls(server._services.ctx.db, record.id)
    return {
        "session_id": record.id,
        "sources": _sources(calls),
        "results": _results(calls),
        "connectors": _connectors(server, record),
        "background": _background(server, record),
    }


def changed_by(frame: dict[str, Any]) -> str | None:
    """Why a turn event changes the summary, or None (session.summary.changed)."""
    name = frame.get("event")
    payload = frame.get("payload") or {}
    if name in TERMINAL:
        return "turn"
    if name == "tool.completed" and payload.get("ok", True):
        tool = str(payload.get("tool") or "")
        if tool in SUMMARY_TOOLS or tool.startswith("mcp."):
            return "tool"
    if name in ("process.started", "process.exited", "workspace.process.changed"):
        return "process"
    return None
