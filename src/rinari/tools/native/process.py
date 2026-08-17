"""Session-scoped process control: start, wait, output, signal, list.

Complements `shell.exec` (one-shot) with long-lived processes the agent can
observe and steer while they run. Handles live only inside one session; the
registry is per-ToolContext. Starting a process goes through the normal
Tool Runtime pipeline as `shell.exec`-class risk; managing an already-started
process is a low-risk follow-up of that approval (policy capability
`process.local`).

POSIX process groups give signal semantics; Windows maps INT/TERM/KILL onto
taskkill.
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
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

from .shell import _BoundedBuffer, _kill_tree

MAX_PROCESS_OUTPUT_BYTES = 512 * 1024
DEFAULT_WAIT_TIMEOUT_S = 60.0
MAX_WAIT_TIMEOUT_S = 1800.0
POSIX_SIGNALS = {
    "INT": 2,
    "SIGINT": 2,
    "TERM": 15,
    "SIGTERM": 15,
    "KILL": 9,
    "SIGKILL": 9,
    "HUP": 1,
    "SIGHUP": 1,
    "CONT": 18,
    "SIGCONT": 18,
}


def _ok(data) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


class _Handle:
    __slots__ = (
        "command",
        "cwd",
        "exit_code",
        "id",
        "process",
        "readers",
        "started_at",
        "stderr",
        "stdout",
    )

    def __init__(self, handle_id: str, command: str, cwd: str, process: subprocess.Popen) -> None:
        self.id = handle_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.stdout = _BoundedBuffer(MAX_PROCESS_OUTPUT_BYTES)
        self.stderr = _BoundedBuffer(MAX_PROCESS_OUTPUT_BYTES)
        self.exit_code: int | None = None
        self.started_at = time.time()
        self.readers: list[threading.Thread] = []


class ProcessRegistry:
    """Thread-safe registry of live session processes."""

    def __init__(self, base_env: dict[str, str] | None = None) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, _Handle] = {}
        self._counter = 0
        self._base_env = dict(base_env or os.environ)

    def start(self, command: str, cwd: str | None = None, env: dict | None = None) -> str:
        kwargs: dict = {"shell": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if cwd:
            kwargs["cwd"] = cwd
        process_env = dict(self._base_env)
        if env:
            process_env.update({str(k): str(v) for k, v in env.items()})
        kwargs["env"] = process_env
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        with self._lock:
            self._counter += 1
            handle_id = f"proc_{self._counter:03d}"
            handle = _Handle(handle_id, command, cwd or "", process)
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                if stream is None:
                    continue
                buffer = handle.stdout if name == "stdout" else handle.stderr
                thread = threading.Thread(
                    target=_pump, args=(stream, buffer, handle, self), daemon=True
                )
                thread.start()
                handle.readers.append(thread)
            self._handles[handle_id] = handle
        return handle_id

    def get(self, handle_id: str) -> _Handle | None:
        with self._lock:
            return self._handles.get(handle_id)

    def list(self) -> list[_Handle]:
        with self._lock:
            return list(self._handles.values())

    def wait(self, handle: _Handle, timeout: float | None) -> bool:
        """Wait for exit; returns True if it exited, False on timeout."""
        try:
            handle.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self._reap(handle)
        return True

    def _reap(self, handle: _Handle) -> None:
        handle.exit_code = handle.process.returncode
        for reader in handle.readers:
            with contextlib.suppress(Exception):
                reader.join(timeout=5)

    def signal(self, handle: _Handle, name: str) -> bool:
        import signal as signal_module

        normalized = name.strip().upper()
        if sys.platform == "win32":
            if normalized in ("INT", "SIGINT", "TERM", "SIGTERM", "KILL", "SIGKILL"):
                _kill_tree(handle.process)
                return True
            return False
        number = POSIX_SIGNALS.get(normalized)
        if number is None:
            return False
        try:
            os.killpg(os.getpgid(handle.process.pid), signal_module.Signals(number))
            return True
        except ProcessLookupError:
            return True  # already gone: signal delivered to a dead group
        except (PermissionError, OSError):
            with contextlib.suppress(OSError):
                handle.process.send_signal(signal_module.Signals(number))
            return True

    def kill(self, handle: _Handle) -> None:
        _kill_tree(handle.process)

    def _forget_dead_if_needed(self, handle: _Handle) -> None:
        """Keep handles until the agent reaps them; nothing to do."""


def _pump(stream, buffer, handle: _Handle, registry: ProcessRegistry) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffer.write(chunk)
    except (OSError, ValueError):
        pass


def _registry(ctx: ToolContext) -> ProcessRegistry | None:
    return ctx.processes if isinstance(ctx.processes, ProcessRegistry) else None


def _resolve_handle(ctx: ToolContext, handle_id: str) -> _Handle | None:
    registry = _registry(ctx)
    if registry is None or not isinstance(handle_id, str) or not handle_id:
        return None
    return registry.get(handle_id)


# -- handlers ---------------------------------------------------------------


def process_start(input: dict, ctx: ToolContext) -> ToolResult:
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
    if cwd:
        try:
            resolved = ctx.sandbox.resolve(cwd, base=ctx.cwd)
            ctx.sandbox.assert_readable(resolved)
            if not resolved.is_dir():
                return _fail(ToolErrorCode.NOT_FOUND, f"cwd is not a directory: {cwd}")
            cwd = str(resolved)
        except Exception as exc:
            return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    handle_id = registry.start(command, cwd=cwd, env=env)
    handle = registry.get(handle_id)
    pid = handle.process.pid if handle is not None else None
    return _ok({"handle": handle_id, "pid": pid, "command": command, "running": True})


def process_wait(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve_handle(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")
    if handle.exit_code is not None:
        return _ok({"handle": handle.id, "exit_code": handle.exit_code, "timed_out": False})
    registry = _registry(ctx)
    assert registry is not None
    # Never block indefinitely: cap an explicit/unset wait so a hung
    # process cannot stall the agent turn forever.
    timeout = input.get("timeout_s")
    try:
        timeout = float(timeout) if timeout is not None else DEFAULT_WAIT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout = DEFAULT_WAIT_TIMEOUT_S
    timeout = min(max(timeout, 0.0), MAX_WAIT_TIMEOUT_S)
    exited = registry.wait(handle, timeout)
    if not exited:
        return _ok({"handle": handle.id, "exit_code": None, "timed_out": True})
    return _ok({"handle": handle.id, "exit_code": handle.exit_code, "timed_out": False})


def process_output(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve_handle(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")
    return _ok(
        {
            "handle": handle.id,
            "running": handle.exit_code is None,
            "exit_code": handle.exit_code,
            "stdout": handle.stdout.text(),
            "stderr": handle.stderr.text(),
            "truncated": handle.stdout.truncated or handle.stderr.truncated,
        }
    )


def process_signal(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve_handle(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")
    if handle.exit_code is not None:
        return _fail(ToolErrorCode.NOT_FOUND, "process already exited")
    name = input.get("signal")
    if not isinstance(name, str) or not name.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "signal must be a non-empty string")
    registry = _registry(ctx)
    assert registry is not None
    if not registry.signal(handle, name):
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"unsupported signal {name!r} on this platform",
        )
    return _ok({"handle": handle.id, "signal": name.strip().upper()})


def process_list(input: dict, ctx: ToolContext) -> ToolResult:
    registry = _registry(ctx)
    if registry is None:
        return _ok({"processes": []})
    entries = []
    for handle in registry.list():
        entries.append(
            {
                "handle": handle.id,
                "command": handle.command,
                "cwd": handle.cwd,
                "running": handle.exit_code is None,
                "exit_code": handle.exit_code,
                "age_s": round(time.time() - handle.started_at, 1),
            }
        )
    return _ok({"processes": entries})


# -- tool definitions ---------------------------------------------------------


def _classify_shell_like(input: dict) -> ClassifiedAction:
    return ClassifiedAction("shell.exec", str(input.get("command") or ""))


def _classify_process_local(input: dict) -> ClassifiedAction:
    return ClassifiedAction("process.local", None)


def process_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="process.start",
            description=(
                "Start a long-lived command and get a handle. Use when you need to "
                "observe or steer a process across turns (servers, watchers, tests)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "env": {"type": "object"},
                },
                "required": ["command"],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=30_000,
            handler=process_start,
            classify=_classify_shell_like,
            namespace="process",
        ),
        ToolDefinition(
            name="process.wait",
            description="Wait for a started process to exit (optionally for up to timeout_s).",
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "timeout_s": {"type": "number", "minimum": 0},
                },
                "required": ["handle"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=600_000,
            handler=process_wait,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.output",
            description="Read accumulated stdout/stderr of a started process.",
            input_schema={
                "type": "object",
                "properties": {"handle": {"type": "string"}},
                "required": ["handle"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=process_output,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.signal",
            description=(
                "Send a signal to a started process (INT, TERM, KILL on all platforms; "
                "HUP, CONT on POSIX)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "signal": {"type": "string"},
                },
                "required": ["handle", "signal"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=30_000,
            handler=process_signal,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.list",
            description="List processes started in this session.",
            input_schema={"type": "object", "properties": {}},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=process_list,
            classify=_classify_process_local,
            namespace="process",
        ),
    ]
