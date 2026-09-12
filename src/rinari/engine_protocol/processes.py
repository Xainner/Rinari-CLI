"""Desktop views of engine-owned processes. Never accepts an arbitrary PID."""

from __future__ import annotations

import subprocess

from rinari.engine_protocol.errors import EngineProtocolError


class DesktopProcesses:
    def __init__(self, server):
        self.server = server

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
    def _row(identity, entry):
        kind, owner, item = entry
        if kind == "pty":
            return {
                "id": identity,
                "kind": kind,
                "command": item["command"],
                "cwd": item["cwd"],
                "running": item["alive"],
                "exit_code": item["exit_code"],
                "can_stop": item["alive"],
            }
        if kind == "preview":
            handle = owner.processes.get(item.process_id) if item.process_id else None
            running = handle.process.poll() is None if handle else item.server is not None
            return {
                "id": identity,
                "kind": kind,
                "command": item.command or "Vista previa HTML",
                "cwd": str(item.root),
                "running": running,
                "url": item.url,
                "exit_code": handle.process.poll() if handle else None,
                "can_stop": bool(handle or item.server),
            }
        command = (
            subprocess.list2cmdline(item.command)
            if isinstance(item.command, list)
            else item.command
        )
        code = item.process.poll()
        return {
            "id": identity,
            "kind": kind,
            "command": command,
            "cwd": item.cwd,
            "pid": item.process.pid,
            "started_at": item.started_at,
            "running": code is None,
            "exit_code": code,
            "can_stop": code is None,
        }

    def list(self, params):
        rows = [self._row(identity, entry) for identity, entry in self._entries(params).items()]
        rows.sort(key=lambda row: (not row["running"], -row.get("started_at", 0)))
        return {"processes": rows[:100], "truncated": len(rows) > 100}

    def _need(self, params):
        identity = params.get("id")
        if not isinstance(identity, str):
            raise EngineProtocolError("INVALID_PARAMS", "id is required")
        entry = self._entries(params).get(identity)
        if entry is None:
            raise EngineProtocolError("NOT_FOUND", "Process does not belong to this session")
        return identity, entry

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
            "process": self._row(identity, entry),
            "stdout": stdout[-64000:],
            "stderr": stderr[-64000:],
            "truncated": truncated or len(stdout) > 64000 or len(stderr) > 64000,
        }

    def stop(self, params):
        identity, entry = self._need(params)
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
