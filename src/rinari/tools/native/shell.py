"""Shell execution with process-tree cancellation and output limits.

The shell is the universal escape hatch (harness.md section 16 of
AGENTS-style policy), always behind the Tool Runtime pipeline. Output is
bounded and spilled by the runtime when it exceeds the threshold.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from typing import Any

from rinari.tools.definition import (
    RISK_HIGH,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

DEFAULT_TIMEOUT_S = 300.0
MAX_OUTPUT_BYTES = 1_000_000


def _ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(
    code: ToolErrorCode, message: str, *, retryable: bool = False, data: Any = None
) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(code=code, message=message, retryable=retryable),
        data=data,
    )


def _drain(stream, buffer: _BoundedBuffer) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffer.write(chunk)
    except (OSError, ValueError):
        pass


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


class _BoundedBuffer:
    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False

    def write(self, data: bytes) -> int:
        if self.size < self._max:
            room = self._max - self.size
            self.chunks.append(data[:room])
            self.size += min(len(data), room)
            if len(data) > room:
                self.truncated = True
        elif data:
            self.truncated = True
        return len(data)

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


def shell_exec(input: dict, ctx: ToolContext) -> ToolResult:
    command = input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "command must be a non-empty string")
    if ctx.cancellation is not None:
        ctx.cancellation.throw_if_cancelled()

    timeout_s = input.get("timeout_s", DEFAULT_TIMEOUT_S)
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    env = dict(os.environ)
    if isinstance(input.get("env"), dict):
        env.update({str(k): str(v) for k, v in input["env"].items()})
    if ctx.environment:
        env.update(ctx.environment)

    cwd_arg = input.get("cwd")
    if cwd_arg:
        if not isinstance(cwd_arg, str):
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "cwd must be a string")
        try:
            cwd_resolved = ctx.sandbox.resolve(cwd_arg, base=ctx.cwd)
            ctx.sandbox.assert_readable(cwd_resolved)
        except Exception as exc:
            return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
        if not cwd_resolved.is_dir():
            return _fail(ToolErrorCode.NOT_FOUND, f"cwd is not a directory: {cwd_arg}")
        cwd: str | None = str(cwd_resolved)
    else:
        cwd = str(ctx.cwd)

    max_bytes = min(ctx.limits.max_output_bytes, MAX_OUTPUT_BYTES)
    stdout = _BoundedBuffer(max_bytes)
    stderr = _BoundedBuffer(max_bytes)

    kwargs: dict[str, Any] = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **kwargs)
    except (OSError, ValueError) as exc:
        return _fail(
            ToolErrorCode.DEPENDENCY_ERROR, f"Failed to start process: {exc.__class__.__name__}"
        )

    readers = []
    if process.stdout is not None:
        readers.append(threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True))
    if process.stderr is not None:
        readers.append(threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True))
    for reader in readers:
        reader.start()

    timed_out = False
    cancelled = False
    cancellation = ctx.cancellation
    try:
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
            process.wait()
    finally:
        if process.poll() is None:
            cancelled = cancellation is not None and cancellation.cancelled
            _kill_tree(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)

    exit_code = process.returncode if process.returncode is not None else -1
    data = {
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
    }
    if cancelled:
        return _fail(ToolErrorCode.CANCELLED, "Command cancelled", data=data)
    if timed_out:
        return _fail(
            ToolErrorCode.TIMEOUT,
            f"Command exceeded {timeout_s:.0f}s and was terminated",
            retryable=True,
            data=data,
        )
    return _ok(data)


def shell_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell.exec",
            description=(
                "Run a command in the session working directory. Returns exit code and "
                "bounded stdout/stderr. Use for builds, tests, git, and anything without a "
                "dedicated structured tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "number", "minimum": 1},
                    "env": {"type": "object"},
                },
                "required": ["command"],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=600_000,
            handler=shell_exec,
            namespace="shell",
            manifest={
                "notes": "executes with the session user; the policy engine decides allow/ask/deny"
            },
        ),
    ]
