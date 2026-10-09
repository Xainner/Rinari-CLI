"""`engine.diagnostics`: what a problem report needs, never what the user wrote.

The desktop bundles this summary with its own logs when someone exports a
diagnostic. It carries versions, data sizes, the state of providers and
extensions, and the latest failed or slow requests. It never carries message
or file contents, credentials, endpoints of private gateways, MCP commands or
request parameters.
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Requests at least this slow are kept even when they succeed.
SLOW_REQUEST_MS = 2_000
_RECENT = 50
# Walking a huge artifact store must not stall the protocol loop.
_MAX_FILES = 20_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class RequestLog:
    """The latest failed or slow requests: method, code and duration only."""

    def __init__(self) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=_RECENT)
        self._lock = threading.Lock()

    def record(self, line: str, response: dict[str, Any] | None, elapsed_ms: int) -> None:
        failed = response is not None and response.get("ok") is False
        if not failed and elapsed_ms < SLOW_REQUEST_MS:
            return
        try:
            method = json.loads(line).get("method")
        except (ValueError, AttributeError):
            method = None
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": method if isinstance(method, str) else "unknown",
            "ms": elapsed_ms,
        }
        if failed:
            entry["code"] = (response or {}).get("error", {}).get("code")
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)


def _bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _tree(root: Path) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    total = files = 0
    try:
        for item in root.rglob("*"):
            if files >= _MAX_FILES:
                return {"bytes": total, "files": files, "truncated": True}
            if item.is_file():
                files += 1
                total += item.stat().st_size
    except OSError:
        pass
    return {"bytes": total, "files": files, "truncated": False}


def _redact_home(text: str) -> str:
    home = str(Path.home())
    return text.replace(home, "~").replace(home.replace("\\", "/"), "~")


def _endpoint_kind(record: Any, product: str) -> str:
    if product != "custom":
        return "known"
    host = (urlsplit(record.endpoint or "").hostname or "").lower()
    return "local" if host in _LOCAL_HOSTS or host.endswith(".localhost") else "custom"


def _storage(services: Any) -> dict[str, Any]:
    db = services.ctx.db
    home = Path(services.ctx.home)
    count = lambda sql: (db.query_one(sql) or [0])[0]  # noqa: E731
    sizes = db.query(
        """
        SELECT s.id AS id,
          (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.id) AS messages,
          (SELECT COALESCE(SUM(LENGTH(m.content) + COALESCE(LENGTH(m.tool_calls_json), 0)), 0)
             FROM session_messages m WHERE m.session_id = s.id) AS message_bytes,
          (SELECT COALESCE(SUM(LENGTH(e.payload_json)), 0)
             FROM session_events e WHERE e.session_id = s.id) AS event_bytes
        FROM sessions s
        ORDER BY message_bytes + event_bytes DESC
        LIMIT 5
        """
    )
    state_db = Path(db.path)
    return {
        "sessions": count("SELECT COUNT(*) FROM sessions"),
        "messages": count("SELECT COUNT(*) FROM session_messages"),
        "events": count("SELECT COUNT(*) FROM session_events"),
        # Ids and sizes only: a title can say what someone is working on.
        "largest_sessions": [
            {
                "session_id": row["id"],
                "messages": row["messages"],
                "message_bytes": row["message_bytes"],
                "event_bytes": row["event_bytes"],
            }
            for row in sizes
        ],
        "files": {
            "state_db": _bytes(state_db),
            "state_db_wal": _bytes(state_db.with_name(state_db.name + "-wal")),
            "history_db": _bytes(home / "history.sqlite"),
            "operations_db": _bytes(home / "engine-operations.sqlite"),
        },
        "artifacts": _tree(home / "artifacts"),
    }


def _providers(services: Any) -> list[dict[str, Any]]:
    from rinari.providers.catalog import product_for

    current = services.providers.current()
    rows = []
    for record in services.providers.list():
        product = product_for(record)
        rows.append(
            {
                "product_id": product,
                "type": record.type,
                "active": current is not None and current.provider.id == record.id,
                "has_credential": services.providers.credential_ref(record) is not None,
                "endpoint": _endpoint_kind(record, product),
                "models": len(services.models.list(record.id)),
            }
        )
    return rows


def _extensions(services: Any, connected: Any) -> dict[str, Any]:
    mcp = [
        {
            "name": row.get("name"),
            "transport": row.get("transport"),
            "scope": row.get("scope") or "global",
            "enabled": bool(row.get("enabled")),
            "connected": bool(connected(row.get("name"))),
        }
        for row in services.mcp.list(None)
    ]
    diagnostics = {row["name"]: row["diagnostics"] for row in services.plugins.doctor()}
    plugins = [
        {
            "name": row.get("name"),
            "version": row.get("version"),
            "source": row.get("source"),
            "enabled": bool(row.get("enabled")),
            "diagnostics": [
                {
                    "code": item.get("code"),
                    "message": _redact_home(str(item.get("message") or ""))[:300],
                }
                for item in diagnostics.get(row.get("name"), [])
            ],
        }
        for row in services.plugins.list()
    ]
    return {"mcp": mcp, "plugins": plugins}


def collect(
    services: Any,
    *,
    engine: dict[str, Any],
    requests: RequestLog,
    connected: Any,
    active_turns: int,
) -> dict[str, Any]:
    """Each section degrades on its own: a broken one reports its error, not the others'."""
    started = time.monotonic()
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine": {
            **engine,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "active_turns": active_turns,
        "recent_requests": requests.entries(),
    }
    for name, section in (
        ("storage", lambda: _storage(services)),
        ("providers", lambda: _providers(services)),
        ("extensions", lambda: _extensions(services, connected)),
    ):
        try:
            report[name] = section()
        except Exception as exc:  # a diagnostic never fails because one part does
            report[name] = {"error": f"{type(exc).__name__}: {_redact_home(str(exc))[:300]}"}
    report["collected_ms"] = int((time.monotonic() - started) * 1000)
    return report
