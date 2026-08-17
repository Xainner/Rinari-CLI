"""PTY support for interactive processes (phase 3 item).

A pty handle pairs a real pseudo-terminal with a coprocess: TTY-aware
applications (shells, editors, prompts) behave as if on a real terminal.
Implementations must use a POSIX pty pair; on platforms without one the
tools fail with a structured DEPENDENCY_ERROR pointing at `process.*`
(same pattern as the LSP fallback: availability is runtime data, not a
silent behavior change).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time

from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_DESTRUCTIVE,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

from .shell import _BoundedBuffer

MAX_PTY_OUTPUT_BYTES = 128 * 1024
DEFAULT_READ_TIMEOUT_S = 1.0
MAX_READ_TIMEOUT_S = 60.0
POSIX_ONLY_ERROR = (
    "PTY requires a POSIX platform (real pty pair); on Windows use "
    "process.start for background commands"
)


def _ok(data) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


class PtyHandle:
    __slots__ = (
        "buffer",
        "command",
        "cwd",
        "exit_code",
        "id",
        "lock",
        "master",
        "process",
        "reaper",
    )

    def __init__(self, handle_id: str, command: str, cwd: str, master: int, process) -> None:
        self.id = handle_id
        self.command = command
        self.cwd = cwd
        self.master = master
        self.process = process
        self.buffer = _BoundedBuffer(MAX_PTY_OUTPUT_BYTES)
        self.exit_code: int | None = None
        self.lock = threading.Lock()
        self.reaper: threading.Thread | None = None

    def pump(self) -> None:
        import select

        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
            except OSError:
                break
            if not ready:
                if self.exited:
                    break
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            with self.lock:
                self.buffer.write(chunk)

    @property
    def exited(self) -> bool:
        return self.exit_code is not None


class PtyRegistry:
    """Session-scoped registry of live pty-backed processes (POSIX only)."""

    def __init__(self, base_env: dict[str, str] | None = None) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, PtyHandle] = {}
        self._counter = 0
        self._base_env = dict(base_env or os.environ)

    @property
    def supported(self) -> bool:
        return hasattr(os, "openpty")

    def start(
        self,
        command: str,
        cwd: str | None,
        env: dict | None,
        columns: int,
        rows: int,
    ) -> str:
        import fcntl
        import struct
        import termios

        master, slave = os.openpty()
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            process_env = dict(self._base_env)
            process_env.setdefault("TERM", "xterm-256color")
            if env:
                process_env.update({str(k): str(v) for k, v in env})
            kwargs: dict = {
                "stdin": slave,
                "stdout": slave,
                "stderr": slave,
                "env": process_env,
                "start_new_session": True,
            }
            if cwd:
                kwargs["cwd"] = cwd
            process = subprocess.Popen(["/bin/sh", "-c", command], **kwargs)
        finally:
            os.close(slave)

        with self._lock:
            self._counter += 1
            handle_id = f"pty_{self._counter:03d}"
            handle = PtyHandle(handle_id, command, cwd or "", master, process)
            self._handles[handle_id] = handle
        handle.reaper = threading.Thread(target=handle.pump, daemon=True)
        handle.reaper.start()
        threading.Thread(target=self._wait, args=(handle_id, handle), daemon=True).start()
        return handle_id

    def get(self, handle_id: str) -> PtyHandle | None:
        with self._lock:
            return self._handles.get(handle_id)

    def list(self) -> list[PtyHandle]:
        with self._lock:
            return list(self._handles.values())

    def _wait(self, handle_id: str, handle: PtyHandle) -> None:
        code = handle.process.wait()
        handle.exit_code = code
        with contextlib.suppress(OSError):
            os.close(handle.master)


def _registry(ctx: ToolContext) -> PtyRegistry | None:
    reg = getattr(ctx, "pty", None)
    return reg if isinstance(reg, PtyRegistry) else None


def _resolve(ctx: ToolContext, handle_id: str) -> PtyHandle | None:
    registry = _registry(ctx)
    if registry is None:
        return None
    if not isinstance(handle_id, str) or not handle_id:
        return None
    return registry.get(handle_id)


# -- handlers ---------------------------------------------------------------


def pty_start(input: dict, ctx: ToolContext) -> ToolResult:
    if sys.platform == "win32" or not hasattr(os, "openpty"):
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, POSIX_ONLY_ERROR)
    registry = _registry(ctx)
    if registry is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "process registry unavailable in this session")
    command = input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "command must be a non-empty string")
    cwd = input.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "cwd must be a string")
    env = input.get("env")
    if env is not None and not isinstance(env, dict):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "env must be an object")
    columns = input.get("columns")
    rows = input.get("rows")
    try:
        columns = int(columns) if columns is not None else 120
        rows = int(rows) if rows is not None else 30
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "columns/rows must be integers")
    columns = min(max(columns, 2), 500)
    rows = min(max(rows, 1), 200)
    if cwd:
        try:
            resolved = ctx.sandbox.resolve(cwd, base=ctx.cwd)
            ctx.sandbox.assert_readable(resolved)
            cwd = str(resolved)
        except Exception as exc:
            return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    handle_id = registry.start(command, cwd, env, columns, rows)
    return _ok({"handle": handle_id, "command": command, "running": True})


def pty_read(input: dict, ctx: ToolContext) -> ToolResult:
    import select

    handle = _resolve(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown pty handle")
    timeout = input.get("timeout_s")
    try:
        timeout = float(timeout) if timeout is not None else DEFAULT_READ_TIMEOUT_S
    except (TypeError, ValueError):
        timeout = DEFAULT_READ_TIMEOUT_S
    timeout = min(max(timeout, 0.0), MAX_READ_TIMEOUT_S)
    data = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([handle.master], [], [], min(remaining, 0.5))
        except OSError:
            break
        if not ready:
            continue
        try:
            chunk = os.read(handle.master, 65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    text = data.decode("utf-8", errors="replace")
    return _ok(
        {
            "handle": handle.id,
            "output": text,
            "truncated": False,
            "exit_code": handle.exit_code,
            "running": not handle.exited,
        }
    )


def pty_write(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown pty handle")
    if handle.exited:
        return _fail(ToolErrorCode.CONFLICT, "process already exited")
    data = input.get("data")
    if not isinstance(data, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "data must be a string")
    if len(data) > 16 * 1024:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "data exceeds 16 KiB per write")
    payload = data.encode("utf-8")
    if not payload.endswith(b"\n") and not payload.endswith(b"\r"):
        payload += b"\n"  # a pty line is only delivered to the reader on a newline
    try:
        written = os.write(handle.master, payload)
    except OSError as exc:
        return _fail(ToolErrorCode.SANDBOX_VIOLATION, str(exc))
    return _ok({"handle": handle.id, "written": written})


def pty_resize(input: dict, ctx: ToolContext) -> ToolResult:
    import fcntl
    import struct
    import termios

    handle = _resolve(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown pty handle")
    rows = input.get("rows")
    columns = input.get("columns")
    try:
        rows = int(rows)
        columns = int(columns)
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "rows/columns must be integers")
    rows = min(max(rows, 1), 200)
    columns = min(max(columns, 2), 500)
    try:
        fcntl.ioctl(handle.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    except OSError as exc:
        return _fail(ToolErrorCode.SANDBOX_VIOLATION, str(exc))
    return _ok({"handle": handle.id, "rows": rows, "columns": columns})


def pty_terminate(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown pty handle")
    if handle.exited:
        return _ok({"handle": handle.id, "exit_code": handle.exit_code})
    try:
        os.killpg(os.getpgid(handle.process.pid), 15)
    except OSError:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(handle.process.pid), 9)
    deadline = time.monotonic() + 5.0
    while not handle.exited and time.monotonic() < deadline:
        time.sleep(0.1)
    if not handle.exited:
        return _fail(ToolErrorCode.TIMEOUT, "process did not exit after TERM/KILL")
    return _ok({"handle": handle.id, "exit_code": handle.exit_code})


def _classify_shell_like(input: dict) -> ClassifiedAction:
    return ClassifiedAction("shell.exec", str(input.get("command") or ""))


def _classify_pty_local(input: dict) -> ClassifiedAction:
    return ClassifiedAction("process.local", None)


def pty_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="pty.start",
            description=(
                "Start a command under a pseudo-terminal (TTY-aware apps: shells, "
                "editors, interactive prompts). POSIX only; on Windows use "
                "process.start."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "env": {"type": "object"},
                    "columns": {"type": "integer", "minimum": 2, "maximum": 500},
                    "rows": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["command"],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=30_000,
            handler=pty_start,
            classify=_classify_shell_like,
            namespace="pty",
        ),
        ToolDefinition(
            name="pty.read",
            description="Read output from a pty handle (waits up to timeout_s).",
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "timeout_s": {"type": "number", "minimum": 0, "maximum": 60},
                },
                "required": ["handle"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            timeout_ms=60_000,
            handler=pty_read,
            classify=_classify_pty_local,
            namespace="pty",
        ),
        ToolDefinition(
            name="pty.write",
            description=(
                "Write keystrokes to a pty handle. A newline is appended if the "
                "payload does not end with one (a pty delivers lines, not bytes)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "data": {"type": "string"},
                },
                "required": ["handle", "data"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=30_000,
            handler=pty_write,
            classify=_classify_pty_local,
            namespace="pty",
        ),
        ToolDefinition(
            name="pty.resize",
            description="Resize the pty window (rows/columns) for TTY-aware apps.",
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "rows": {"type": "integer", "minimum": 1, "maximum": 200},
                    "columns": {"type": "integer", "minimum": 2, "maximum": 500},
                },
                "required": ["handle", "rows", "columns"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=pty_resize,
            classify=_classify_pty_local,
            namespace="pty",
        ),
        ToolDefinition(
            name="pty.terminate",
            description="Terminate a pty process group (TERM, then KILL).",
            input_schema={
                "type": "object",
                "properties": {"handle": {"type": "string"}},
                "required": ["handle"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_DESTRUCTIVE,
            idempotent=False,
            timeout_ms=30_000,
            handler=pty_terminate,
            classify=_classify_pty_local,
            namespace="pty",
        ),
    ]
