"""Desktop views of engine-owned processes. Never accepts an arbitrary PID."""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from rinari.engine_protocol.errors import (
    STALE_RESOURCE,
    EngineProtocolError,
)

LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 200
# Opaque offset cursors ("o{N}"). ASCII digits only (\d would accept
# Unicode digits) and bounded so int() cannot raise.
_CURSOR_RE = re.compile(r"o[0-9]{1,6}")
# Loopback TCP probes backing the `readiness` field. Bounded by design:
# loopback only, no HTTP bytes, one second, cached per resource.
READINESS_TTL_S = 10.0
READINESS_TIMEOUT_S = 1.0

READINESS_UNKNOWN = "unknown"
READINESS_LISTENING = "listening"
READINESS_NOT_LISTENING = "not_listening"


def _exit_reason(code: Any, stop_requested: bool) -> str | None:
    """Classify how a resource finished. None while running."""
    if code is None:
        return None
    if stop_requested:
        return "stopped"
    if code == 0:
        return "exited"
    if isinstance(code, int) and code < 0:
        return "signaled"
    if isinstance(code, int) and code > 0:
        return "failed"
    return "unknown"


def _probe_loopback(url: str) -> str:
    """TCP-level listen check for loopback http(s) URLs only.

    Returns "listening" when something accepts the connection. Anything
    else is "not_listening"; non-loopback or unparsable URLs are never
    probed and report "unknown". Listening is not app readiness.

    Only literal loopback IPs (127/8, ::1) and bare "localhost" are
    dialed. DNS names are never resolved here: a name such as
    127.0.0.1.evil.com must not turn this probe into an external scan.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return READINESS_UNKNOWN
    if parsed.scheme not in ("http", "https"):
        return READINESS_UNKNOWN
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        dial = "127.0.0.1"
    elif _is_loopback_literal(host):
        dial = host
    else:
        return READINESS_UNKNOWN
    try:
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return READINESS_UNKNOWN
    try:
        with socket.create_connection((dial, port), timeout=READINESS_TIMEOUT_S):
            return READINESS_LISTENING
    except OSError:
        return READINESS_NOT_LISTENING


def _is_loopback_literal(host: str) -> bool:
    """True only for literal loopback IPs, never DNS names."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


class DesktopProcesses:
    def __init__(self, server):
        self.server = server
        # resource identity -> (readiness, checked_at); loopback only.
        self._readiness: dict[str, tuple[str, float]] = {}

    @property
    def _engine_instance_id(self) -> str:
        return getattr(self.server, "_engine_instance_id", "unknown")

    def _entries(self, params):
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError("INVALID_PARAMS", "session_id is required")
        self.server._services.sessions.show(session_id)
        entries = {}
        registry = self.server._turns._desktop_processes.get(session_id)
        if registry:
            for handle in registry.list():
                entries[f"process:{handle.id}"] = ("process", registry, handle)
        for row in self.server._pty.list():
            if row["session_id"] == session_id:
                entries[f"pty:{row['pty_id']}"] = ("pty", self.server._pty, row)
        previews = self.server._previews
        with previews._lock:
            for item in previews._items.values():
                if item.session_id == session_id:
                    entries[f"preview:{item.id}"] = ("preview", previews, item)
        return entries

    @staticmethod
    def _generation(identity, entry) -> int:
        kind, owner, item = entry
        if kind == "process":
            return int(getattr(owner, "generation", 1) or 1)
        if kind == "pty":
            handle_generation = getattr(owner, "handle_generation", None)
            if callable(handle_generation):
                with contextlib.suppress(Exception):
                    return int(handle_generation(item["pty_id"]) or 1)
            return 1
        return 1

    def _readiness_for(self, identity: str, url: Any, *, refresh: bool) -> tuple[str, float | None]:
        if not isinstance(url, str) or not url:
            return READINESS_UNKNOWN, None
        cached = self._readiness.get(identity)
        now = time.time()
        if cached is not None and not refresh and now - cached[1] < READINESS_TTL_S:
            return cached
        if not refresh:
            if cached is not None:
                return cached
            return READINESS_UNKNOWN, None
        state = _probe_loopback(url)
        if state == READINESS_UNKNOWN and cached is not None:
            return cached
        checked_at = now
        if state == READINESS_UNKNOWN:
            return READINESS_UNKNOWN, None
        self._readiness[identity] = (state, checked_at)
        return state, checked_at

    def _row(self, identity, entry, *, refresh_readiness: bool = False):
        kind, owner, item = entry
        instance_id = self._engine_instance_id
        generation = self._generation(identity, entry)
        if kind == "pty":
            running = bool(item["alive"])
            code = item["exit_code"]
            readiness, checked_at = self._readiness_for(identity, None, refresh=False)
            return {
                "id": identity,
                "kind": kind,
                "command": item["command"],
                "cwd": item["cwd"],
                "running": running,
                "exit_code": code,
                "ended_at": item.get("ended_at"),
                "exit_reason": (
                    None
                    if running
                    else (_exit_reason(code, bool(item.get("stop_requested"))) or "unknown")
                ),
                "can_stop": item["alive"],
                "generation": generation,
                "engine_instance_id": instance_id,
                "readiness": readiness,
                "readiness_checked_at": checked_at,
            }
        if kind == "preview":
            handle = owner.processes.get(item.process_id) if item.process_id else None
            running = handle.process.poll() is None if handle else item.server is not None
            code = handle.process.poll() if handle else None
            readiness, checked_at = self._readiness_for(
                identity, item.url, refresh=refresh_readiness
            )
            return {
                "id": identity,
                "kind": kind,
                "command": item.command or "Vista previa HTML",
                "cwd": str(item.root),
                "running": running,
                "url": item.url,
                "exit_code": code,
                "ended_at": None,
                "exit_reason": None if running else (_exit_reason(code, False) or "unknown"),
                "can_stop": bool(handle or item.server),
                "generation": generation,
                "engine_instance_id": instance_id,
                "readiness": readiness,
                "readiness_checked_at": checked_at,
            }
        command = (
            subprocess.list2cmdline(item.command)
            if isinstance(item.command, list)
            else item.command
        )
        code = item.process.poll()
        running = code is None
        readiness, checked_at = self._readiness_for(identity, None, refresh=False)
        return {
            "id": identity,
            "kind": kind,
            "command": command,
            "cwd": item.cwd,
            "pid": item.process.pid,
            "started_at": item.started_at,
            "running": running,
            "exit_code": code,
            "ended_at": getattr(item, "ended_at", None),
            "exit_reason": (
                None
                if running
                else (_exit_reason(code, bool(getattr(item, "stop_requested", False))) or "unknown")
            ),
            "can_stop": code is None,
            "generation": generation,
            "engine_instance_id": instance_id,
            "readiness": readiness,
            "readiness_checked_at": checked_at,
        }

    @staticmethod
    def _pagination(params, total: int) -> tuple[int, int, str | None]:
        limit = params.get("limit", LIST_DEFAULT_LIMIT)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= LIST_MAX_LIMIT
        ):
            raise EngineProtocolError(
                "INVALID_PARAMS", f"Param 'limit' must be an int in 1..{LIST_MAX_LIMIT}."
            )
        cursor = params.get("cursor")
        offset = 0
        if cursor is not None:
            # Strict shape before int(): isdigit() accepts unicode digits
            # and unbounded lengths that int() then rejects with ValueError.
            if not isinstance(cursor, str) or _CURSOR_RE.fullmatch(cursor) is None:
                raise EngineProtocolError("INVALID_PARAMS", "Param 'cursor' is malformed.")
            offset = int(cursor[1:])
            if offset > total:
                offset = total
        end = min(offset + limit, total)
        next_cursor = f"o{end}" if end < total else None
        return offset, end, next_cursor

    def list(self, params):
        entries = self._entries(params)
        identities = sorted(entries)
        # Presentation order is deterministic per snapshot, but snapshots
        # are point-in-time: a cursor is only valid against a stable total.
        # Consumers paging live registries must re-list when `total`
        # changes between pages instead of assuming offset stability.
        all_rows = [self._row(identity, entries[identity]) for identity in identities]
        all_rows.sort(key=lambda row: (not row["running"], -row.get("started_at", 0)))
        total = len(all_rows)
        offset, end, next_cursor = self._pagination(params, total)
        return {
            "processes": all_rows[offset:end],
            "truncated": next_cursor is not None,
            "total": total,
            "next_cursor": next_cursor,
            "engine_instance_id": self._engine_instance_id,
        }

    def _need(self, params):
        identity = params.get("id")
        if not isinstance(identity, str):
            raise EngineProtocolError("INVALID_PARAMS", "id is required")
        entry = self._entries(params).get(identity)
        if entry is None:
            raise EngineProtocolError("NOT_FOUND", "Process does not belong to this session")
        return identity, entry

    def _check_preconditions(self, params, identity, entry) -> None:
        """Validate optional stop preconditions from process_identity_v1."""
        expected_instance = params.get("engine_instance_id")
        if expected_instance is not None:
            if not isinstance(expected_instance, str) or not expected_instance:
                raise EngineProtocolError(
                    "INVALID_PARAMS", "Param 'engine_instance_id' must be a string."
                )
            if expected_instance != self._engine_instance_id:
                raise EngineProtocolError(
                    STALE_RESOURCE,
                    "Engine restarted since this resource was observed.",
                    details={
                        "identity": identity,
                        "expected_instance": expected_instance,
                        "engine_instance_id": self._engine_instance_id,
                    },
                )
        expected_generation = params.get("generation")
        if expected_generation is not None:
            if not isinstance(expected_generation, int) or isinstance(expected_generation, bool):
                raise EngineProtocolError("INVALID_PARAMS", "Param 'generation' must be an int.")
            if expected_generation != self._generation(identity, entry):
                raise EngineProtocolError(
                    STALE_RESOURCE,
                    "Resource identity changed since it was observed.",
                    details={
                        "identity": identity,
                        "expected_generation": expected_generation,
                        "generation": self._generation(identity, entry),
                    },
                )

    def read(self, params):
        identity, entry = self._need(params)
        kind, owner, item = entry
        stdout, stderr, truncated = "", "", False
        if kind == "pty":
            output = owner.read(item["pty_id"])
            stdout, truncated = output["data"], output["truncated"]
        else:
            handle = (
                owner.processes.get(item.process_id)
                if kind == "preview" and item.process_id
                else (item if kind == "process" else None)
            )
            if handle:
                stdout = getattr(handle.stdout, "tail_text", handle.stdout.text)()
                stderr = getattr(handle.stderr, "tail_text", handle.stderr.text)()
                truncated = any(
                    buffer.truncated or getattr(buffer, "tail_truncated", False)
                    for buffer in (handle.stdout, handle.stderr)
                )
        return {
            "process": self._row(identity, entry, refresh_readiness=True),
            "stdout": stdout[-64000:],
            "stderr": stderr[-64000:],
            "truncated": truncated or len(stdout) > 64000 or len(stderr) > 64000,
            "engine_instance_id": self._engine_instance_id,
        }

    def stop(self, params):
        identity, entry = self._need(params)
        self._check_preconditions(params, identity, entry)
        kind, owner, item = entry
        if kind == "preview":
            handle = owner.processes.get(item.process_id) if item.process_id else None
            owner.stop({"session_id": params["session_id"], "preview_id": item.id})
            if handle:
                owner.processes.wait(handle, 2)
            return {"id": identity, "running": handle.process.poll() is None if handle else False}
        if kind == "pty":
            result = owner.terminate(item["pty_id"])
            return {"id": identity, "running": result["alive"]}
        if item.process.poll() is None:
            owner.kill(item)
            owner.wait(item, 2)
        return {"id": identity, "running": item.process.poll() is None}
