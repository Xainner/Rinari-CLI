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
import time
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
MAX_CAPTURE_BYTES = 50 * 1024 * 1024


def resolve_argv(command: Any, env: dict[str, str]) -> Any:
    """Find argv[0] on PATH the way a Windows shell would.

    Without a shell, CreateProcess only finds `.exe` files: `npm`, `npx` or
    `yarn` are `.cmd` scripts, so `argv: ["npm", "test"]` failed with
    FileNotFoundError before running anything. `shutil.which` applies PATHEXT
    against the PATH the process will get. Only PATH is searched, like
    PowerShell: a `git.cmd` inside the project must not stand in for git. A
    name with a directory, or one that is not found, is left alone and fails
    exactly as before.
    """
    if sys.platform != "win32" or not isinstance(command, list) or not command:
        return command
    program = command[0]
    if not program or os.path.dirname(program):
        return command
    import shutil

    search = env.get("PATH") or env.get("Path") or os.environ.get("PATH", "")
    found = shutil.which(program, path=search)
    return [found, *command[1:]] if found else command


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


def _drain(stream, buffer: _BoundedBuffer, name: str, sink=None, capture=None) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            # BufferedReader.read(n) may wait for n bytes or EOF.  A process
            # that prints a short line and then waits would therefore never
            # reach the live sink.  read1 asks the pipe for whatever is
            # available now while retaining the bounded drain.
            reader = getattr(stream, "read1", stream.read)
            chunk = reader(65536)
            if not chunk:
                break
            admitted = capture.write(name, chunk) if capture is not None else chunk
            buffer.write(admitted)
            if sink is not None and admitted:
                with contextlib.suppress(Exception):  # display never breaks the drain
                    text = decoder.decode(admitted, final=False)
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


class _ExecutionCapture:
    """A shared byte budget for both output streams, independent of UI limits."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.remaining = MAX_CAPTURE_BYTES
        self.streams = {"stdout": [], "stderr": []}
        self.truncated = False

    def write(self, name: str, chunk: bytes) -> bytes:
        with self.lock:
            admitted = chunk[: self.remaining]
            self.remaining -= len(admitted)
            self.streams[name].append(admitted) if admitted else None
            self.truncated |= len(admitted) < len(chunk)
            return admitted

    def result(self) -> dict:
        return {
            **{
                name: b"".join(chunks).decode("utf-8", errors="replace")
                for name, chunks in self.streams.items()
            },
            "truncated": self.truncated,
        }


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


def effective_timeout(requested: Any, default: float, ctx: ToolContext) -> dict[str, Any]:
    """Seconds a wait may last and where that number came from.

    The tool's own deadline (its timeout_ms metadata, narrowed by the turn)
    caps the request: a handler that waits past it would outlive the limit
    the runtime promised. ``source`` lets the model and the UI say why a
    command stopped instead of guessing at a fixed cap.
    """
    try:
        value = float(requested) if requested is not None else default
        source = "argument" if requested is not None else "default"
    except (TypeError, ValueError):
        value, source = default, "default"
    if ctx.deadline_at is not None:
        remaining = max(0.0, ctx.deadline_at - time.time())
        if remaining < value:
            value, source = remaining, "deadline"
    return {
        "requested_s": requested if isinstance(requested, (int, float)) else None,
        "effective_s": round(value, 3),
        "source": source,
    }


def timeout_message(timeout: dict[str, Any]) -> str:
    seconds = f"{timeout['effective_s']:.0f}s"
    if timeout["source"] == "default":
        return f"Command exceeded the default {seconds} (timeout_s was not set) and was terminated"
    if timeout["source"] == "deadline":
        return (
            f"Command reached the tool's execution limit after {seconds} and was terminated; "
            "use background=true and process.wait for longer work"
        )
    return f"Command exceeded timeout_s={seconds} and was terminated"


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

    timeout = effective_timeout(input.get("timeout_s"), DEFAULT_TIMEOUT_S, ctx)
    timeout_s = timeout["effective_s"]
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
    capture = _ExecutionCapture()

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
        process = subprocess.Popen(resolve_argv(command, env), **kwargs)
    except (OSError, ValueError) as exc:
        return _fail(
            ToolErrorCode.DEPENDENCY_ERROR, f"Failed to start process: {exc.__class__.__name__}"
        )

    sink = ctx.output_sink
    readers = []
    if process.stdout is not None:
        readers.append(
            threading.Thread(
                target=_drain, args=(process.stdout, stdout, "stdout", sink, capture), daemon=True
            )
        )
    if process.stderr is not None:
        readers.append(
            threading.Thread(
                target=_drain, args=(process.stderr, stderr, "stderr", sink, capture), daemon=True
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
        "cwd": cwd,
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
        "timeout": timeout,
    }
    if cancelled:
        result = _fail(ToolErrorCode.CANCELLED, "Command cancelled", data=data)
    elif timed_out:
        result = _fail(
            ToolErrorCode.TIMEOUT,
            timeout_message(timeout),
            retryable=True,
            data=data,
        )
    else:
        result = _ok(data)
    from dataclasses import replace

    return replace(result, captured_output=capture.result() if data["truncated"] else None)


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
                    "timeout_s": {
                        "type": "number",
                        "minimum": 1,
                        "description": "Seconds; default 60, max 600 (longer: background=true).",
                    },
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
