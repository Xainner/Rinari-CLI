"""Shell execution with process-tree cancellation and output limits.

The shell is the universal escape hatch (harness.md section 16 of
AGENTS-style policy), always behind the Tool Runtime pipeline. Output is
bounded and spilled by the runtime when it exceeds the threshold.
"""

from __future__ import annotations

import codecs
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

DEFAULT_TIMEOUT_S = 60.0
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


def _drain(stream, buffer: _BoundedBuffer, name: str, sink=None) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buffer.write(chunk)
            if sink is not None:
                with contextlib.suppress(Exception):  # display never breaks the drain
                    text = decoder.decode(chunk, final=False)
                    if text:
                        sink(name, text)
        if sink is not None:
            with contextlib.suppress(Exception):
                tail = decoder.decode(b"", final=True)
                if tail:
                    sink(name, tail)
    except (OSError, ValueError):
        pass


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
        with contextlib.suppress(OSError):
            process.kill()
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
        return codecs.getincrementaldecoder("utf-8")(errors="replace").decode(
            b"".join(self.chunks), final=False
        )


def shell_exec(input: dict, ctx: ToolContext) -> ToolResult:
    command = input.get("argv") if "argv" in input else input.get("command")
    if isinstance(command, list):
        if not command or not all(isinstance(s, str) and "\0" not in s for s in command):
            return _fail(
                ToolErrorCode.INVALID_ARGUMENT, "argv must be non-empty strings without NUL"
            )
    elif not isinstance(command, str) or not command.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "command or argv is required")
    if "argv" in input and "command" in input:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Use command or argv, not both")
    if input.get("background"):
        from rinari.tools.native.process import process_start

        return process_start(input, ctx)
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
        "shell": not isinstance(command, list),
        # shell.exec is deliberately non-interactive. In particular this
        # prevents ssh from inheriting an unusable stdin and waiting forever.
        "stdin": subprocess.DEVNULL,
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

    sink = ctx.output_sink
    readers = []
    if process.stdout is not None:
        readers.append(
            threading.Thread(
                target=_drain, args=(process.stdout, stdout, "stdout", sink), daemon=True
            )
        )
    if process.stderr is not None:
        readers.append(
            threading.Thread(
                target=_drain, args=(process.stderr, stderr, "stderr", sink), daemon=True
            )
        )
    for reader in readers:
        reader.start()

    timed_out = False
    cancelled = False
    cancellation = ctx.cancellation
    remove_cancel_callback = None
    if cancellation is not None:

        def terminate_on_cancel() -> None:
            if process.poll() is None:
                _kill_tree(process)

        remove_cancel_callback = cancellation.on_cancel(terminate_on_cancel)
    try:
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
    finally:
        if remove_cancel_callback is not None:
            remove_cancel_callback()
        cancelled = cancellation is not None and cancellation.cancelled
        if process.poll() is None:
            _kill_tree(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)

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
                "Run command or argv in the session working directory. Prefer argv for literal "
                "arguments without shell quoting; background=true returns a process handle. "
                "Returns exit code and "
                "bounded stdout/stderr. Use for builds, tests, git, and anything without a "
                "dedicated structured tool. This is non-interactive (stdin is closed). "
                "On Windows the command runs through the configured Windows command shell; "
                "use Windows-compatible commands. Set a short explicit timeout for SSH and "
                "network probes, and an explicit longer timeout for builds/tests."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 256,
                    },
                    "background": {"type": "boolean", "default": False},
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "number", "minimum": 1},
                    "env": {"type": "object"},
                },
                "oneOf": [{"required": ["command"]}, {"required": ["argv"]}],
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
