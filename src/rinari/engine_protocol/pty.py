"""Engine-owned PTY sessions (docs/desktop 05-A).

One EnginePtyService per engine process promotes the tool-layer
PtyRegistry mechanics — same backend, no second implementation — to
protocol sessions: start/write/resize/read/list/terminate plus
pty.output/pty.exit events on the shared outbox (the desktop renders
xterm from these, never polls in a loop).

Trust model: starting a PTY runs an arbitrary shell command, so start
is user-initiated by construction (the command travels visibly in the
call, like opening a local terminal) and is additionally contained:
POSIX only (PTY_UNSUPPORTED elsewhere, never a fake shell), cwd must be
a real directory and is never the home root, env overrides are capped.
Handles die with their process; engine shutdown terminates stragglers
and a restart reports none (desktop shows "terminal ended", never a
frozen ghost).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import PTY_UNSUPPORTED, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.shared.errors import NotFoundError, PermissionDeniedError
from rinari.tools.native.ptytools import POSIX_ONLY_ERROR, PtyRegistry

_WRITE_LIMIT = 16 * 1024
_ENV_LIMIT = 64
_ENV_VALUE_LIMIT = 8 * 1024
_FORWARD_INTERVAL_S = 0.2
_TERMINATE_GRACE_S = 5.0


class EnginePtyService:
    """One registry + forwarder set per engine process."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        home: Path | None = None,
        resolve_session: Callable[[str], Any] | None = None,
    ) -> None:
        self._registry = PtyRegistry()
        self._emit = emit
        self._home = home
        self._resolve_session = resolve_session
        self._lock = threading.Lock()
        self._forwarders: set[str] = set()
        self._session_of: dict[str, str | None] = {}

    # -- lifecycle ------------------------------------------------------

    def start(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        columns: int = 80,
        rows: int = 24,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        # Parameter validation is platform-independent (a bad request is
        # INVALID_PARAMS even where no real PTY exists); only the spawn
        # itself requires POSIX.
        if not isinstance(command, str) or not command.strip():
            raise EngineProtocolError(
                "INVALID_PARAMS", "Param 'command' must be a non-empty string."
            )
        directory = self._resolve_cwd(cwd, session_id)
        columns = self._clamp_dimension(columns, "columns", 2, 500, 80)
        rows = self._clamp_dimension(rows, "rows", 1, 200, 24)
        clean_env = self._clean_env(env)
        if not self._registry.supported:
            raise EngineProtocolError(PTY_UNSUPPORTED, POSIX_ONLY_ERROR)
        pty_id = self._registry.start(command, str(directory), clean_env, columns, rows)
        with self._lock:
            self._session_of[pty_id] = session_id
            first = pty_id not in self._forwarders
            if first:
                self._forwarders.add(pty_id)
        if first:
            thread = threading.Thread(target=self._forward, args=(pty_id,), daemon=True)
            thread.start()
        return {"pty_id": pty_id, "session_id": session_id}

    def write(self, pty_id: str, data: str) -> dict[str, Any]:
        handle = self._need(pty_id)
        if handle.exited:
            raise EngineProtocolError("INVALID_PARAMS", f"PTY already exited: {pty_id}")
        if not isinstance(data, str):
            raise EngineProtocolError("INVALID_PARAMS", "Param 'data' must be a string.")
        if len(data) > _WRITE_LIMIT:
            raise EngineProtocolError("INVALID_PARAMS", "Param 'data' exceeds 16 KiB.")
        payload = data.encode("utf-8")
        if not payload.endswith(b"\n") and not payload.endswith(b"\r"):
            payload += b"\n"  # a pty line only reaches the reader on a newline
        try:
            written = os.write(handle.master, payload)
        except OSError as exc:
            raise EngineProtocolError("ENGINE_ERROR", str(exc)) from None
        return {"pty_id": pty_id, "written": written}

    def resize(self, pty_id: str, columns: int, rows: int) -> dict[str, Any]:
        # Handle first: unknown ids are NOT_FOUND on every platform, even
        # where the fcntl import below cannot load.
        handle = self._need(pty_id)
        columns = self._clamp_dimension(columns, "columns", 2, 500, 80)
        rows = self._clamp_dimension(rows, "rows", 1, 200, 24)

        import fcntl
        import struct
        import termios

        try:
            fcntl.ioctl(handle.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        except OSError as exc:
            raise EngineProtocolError("ENGINE_ERROR", str(exc)) from None
        return {"pty_id": pty_id, "columns": columns, "rows": rows}

    def read(self, pty_id: str) -> dict[str, Any]:
        handle = self._need(pty_id)
        with handle.lock:
            text = handle.buffer.text()
            truncated = handle.buffer.truncated
        return {
            "pty_id": pty_id,
            "alive": not handle.exited,
            "exit_code": handle.exit_code,
            "truncated": truncated,
            "data": text,
        }

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            session_of = dict(self._session_of)
        return [
            {
                "pty_id": handle.id,
                "command": handle.command,
                "cwd": handle.cwd,
                "alive": not handle.exited,
                "exit_code": handle.exit_code,
                "session_id": session_of.get(handle.id),
            }
            for handle in self._registry.list()
        ]

    def terminate(self, pty_id: str) -> dict[str, Any]:
        handle = self._need(pty_id)
        if handle.exited:
            # No-op success on a dead handle (desktop needs no tombstones).
            return {"pty_id": pty_id, "alive": False, "exit_code": handle.exit_code}
        try:
            os.killpg(os.getpgid(handle.process.pid), 15)
        except OSError:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(handle.process.pid), 9)
        deadline = time.monotonic() + _TERMINATE_GRACE_S
        while not handle.exited and time.monotonic() < deadline:
            time.sleep(0.1)
        return {"pty_id": pty_id, "alive": not handle.exited, "exit_code": handle.exit_code}

    def shutdown(self) -> None:
        for handle in self._registry.list():
            if not handle.exited:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(handle.process.pid), 9)

    # -- internals ------------------------------------------------------

    def _need(self, pty_id: str):
        handle = self._registry.get(pty_id)
        if handle is None:
            raise NotFoundError(f"Unknown pty: {pty_id}")
        return handle

    def _resolve_cwd(self, cwd: str | None, session_id: str | None) -> Path:
        if session_id is not None:
            if self._resolve_session is None:
                raise EngineProtocolError(
                    "INVALID_PARAMS", "Param 'session_id' is not supported here."
                )
            record = self._resolve_session(session_id)
            if cwd is None:
                cwd = record.current_cwd
        if not isinstance(cwd, str) or not cwd:
            raise EngineProtocolError(
                "INVALID_PARAMS", "Param 'cwd' (or a session default) is required."
            )
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise EngineProtocolError("INVALID_PARAMS", f"Param 'cwd' is not a directory: {cwd}")
        if self._home is not None and directory == self._home.expanduser().resolve():
            raise PermissionDeniedError(
                "$HOME is never an implicit PTY workspace",
                hint="Start the terminal in a project subdirectory instead.",
            )
        return directory

    @staticmethod
    def _clamp_dimension(value: Any, name: str, low: int, high: int, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise EngineProtocolError("INVALID_PARAMS", f"Param '{name}' must be an integer.")
        return min(max(value, low), high)

    @staticmethod
    def _clean_env(env: Any) -> dict[str, str] | None:
        if env is None:
            return None
        if not isinstance(env, dict):
            raise EngineProtocolError("INVALID_PARAMS", "Param 'env' must be an object.")
        if len(env) > _ENV_LIMIT:
            raise EngineProtocolError(
                "INVALID_PARAMS", f"Param 'env' exceeds {_ENV_LIMIT} entries."
            )
        clean: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise EngineProtocolError(
                    "INVALID_PARAMS", "Param 'env' keys and values must be strings."
                )
            if len(value) > _ENV_VALUE_LIMIT:
                raise EngineProtocolError("INVALID_PARAMS", f"Param 'env[{key}]' exceeds 8 KiB.")
            clean[key] = value
        return clean

    def _forward(self, pty_id: str) -> None:
        """Push new output as pty.output events, then exactly one pty.exit."""
        handle = self._registry.get(pty_id)
        if handle is None:
            return
        offset = 0
        while True:
            with handle.lock:
                text = handle.buffer.text()
            if len(text) > offset:
                self._emit(
                    event(
                        "pty.output",
                        {
                            "pty_id": pty_id,
                            "session_id": self._session_of.get(pty_id),
                            "data": text[offset:],
                        },
                    )
                )
                offset = len(text)
            if handle.exited:
                with self._lock:
                    self._forwarders.discard(pty_id)
                self._emit(
                    event(
                        "pty.exit",
                        {
                            "pty_id": pty_id,
                            "session_id": self._session_of.get(pty_id),
                            "exit_code": handle.exit_code,
                        },
                    )
                )
                return
            time.sleep(_FORWARD_INTERVAL_S)
