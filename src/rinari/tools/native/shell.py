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

from rinari.tools import normalize
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
# #32: a foreground command that reaches its time limit moves to the
# background instead of dying, unless the model asked for a short limit (it
# expected a quick answer) or the command waits for a person (ssh, editors).
SHORT_EXPLICIT_TIMEOUT_S = 30.0
_INTERACTIVE = ("vim", "vi", "nano", "emacs", "less", "more", "top", "htop", "man")


class ForegroundRun:
    """A shell.exec in the foreground that the owner can send to the background."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.reason = ""

    def to_background(self, reason: str) -> None:
        self.reason = reason
        self.requested.set()


_FOREGROUND: dict[tuple[str, str], ForegroundRun] = {}
_FOREGROUND_LOCK = threading.Lock()


def request_background(session_id: str, tool_call_id: str, reason: str = "owner") -> bool:
    """Move a running foreground command to the background (#32). False if none."""
    with _FOREGROUND_LOCK:
        run = _FOREGROUND.get((session_id, tool_call_id))
    if run is None:
        return False
    run.to_background(reason)
    return True


def running_foreground(session_id: str) -> list[str]:
    """Tool call ids of this session's commands running in the foreground."""
    with _FOREGROUND_LOCK:
        return [call for (sid, call) in _FOREGROUND if sid == session_id]


def _waits_for_a_person(command: Any) -> bool:
    text = command if isinstance(command, str) else " ".join(map(str, command or []))
    words = text.strip().split()
    if not words:
        return False
    program = os.path.basename(words[0]).lower().removesuffix(".exe")
    if program in _INTERACTIVE:
        return True
    # ssh with no remote command opens an interactive shell.
    return program == "ssh" and len([w for w in words[1:] if not w.startswith("-")]) <= 1


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


SHELLS = ("default", "cmd", "powershell", "bash", "sh")


def shell_argv(command: Any, shell: Any) -> Any:
    """`command` run by the requested shell, as argv; unchanged for the default.

    Models write bash or PowerShell while Windows runs cmd.exe: `;`, `| head`
    and `$var` failed there. Choosing the shell is explicit and only wraps the
    command at launch, so policy still classifies the command text itself.
    Raises ValueError with what to do when the shell is not available.
    """
    if not isinstance(command, str) or shell in (None, "", "default"):
        return command
    import shutil

    if shell == "powershell":
        program = shutil.which("pwsh") or shutil.which("powershell")
        if not program:
            raise ValueError("PowerShell is not installed; use the default shell")
        return [program, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        if sys.platform != "win32":
            raise ValueError("cmd is only available on Windows")
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command]
    if shell in ("bash", "sh"):
        program = _bash() if shell == "bash" else shutil.which("sh") or _bash()
        if not program:
            raise ValueError(f"{shell} is not installed; use the default shell")
        return [program, "-c", command]
    raise ValueError(f"shell must be one of {', '.join(SHELLS)}")


def _bash() -> str | None:
    import shutil

    if sys.platform == "win32":
        # Git for Windows' bash, not the WSL launcher in System32: that one
        # runs inside a Linux distribution that cannot see this session's paths.
        # Prefer the bin/bash.exe launcher: it sets up the PATH for grep,
        # head and the other tools. git.exe lives in cmd/ or mingw64/bin/.
        git = shutil.which("git")
        if git:
            base = os.path.dirname(os.path.dirname(git))
            for root in (base, os.path.dirname(base)):
                path = os.path.join(root, "bin", "bash.exe")
                if os.path.isfile(path):
                    return path
        found = shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    return shutil.which("bash")


SHELL_SCHEMA = {
    "type": "string",
    "enum": list(SHELLS),
    "description": (
        "Shell for `command`: default is cmd.exe on Windows and /bin/sh elsewhere; "
        "powershell or bash run the command with that syntax."
    ),
}


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


def _terminate(process: subprocess.Popen, job: Any) -> None:
    """End a command and everything it started: the job, else the process tree."""
    if job is not None and job.terminate():
        return
    _kill_tree(process)


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # 1 s was not enough with the machine loaded (#90).
                timeout=5.0,
                check=False,
            )
        with contextlib.suppress(OSError):
            if process.poll() is None:
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
    try:
        command = shell_argv(command, input.get("shell"))
    except ValueError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
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
    # The same buffers as a background process: if this command moves to the
    # background (#32), the registry takes them with the output so far.
    from rinari.tools.native.process import _ProcessBuffer

    stdout = _ProcessBuffer(max_bytes)
    stderr = _ProcessBuffer(max_bytes)
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

    from rinari.tools.native.jobs import popen_in_job

    try:
        process, job = popen_in_job(resolve_argv(command, env), **kwargs)
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
                _terminate(process, job)

        remove_cancel_callback = cancellation.on_cancel(terminate_on_cancel)
    registry = getattr(ctx, "processes", None)
    from rinari.tools.native.process import ProcessRegistry

    can_background = isinstance(registry, ProcessRegistry) and bool(ctx.tool_call_id)
    run = ForegroundRun()
    key = (ctx.session_id, ctx.tool_call_id)
    if can_background:
        with _FOREGROUND_LOCK:
            _FOREGROUND[key] = run
    explicit = timeout["source"] == "argument"
    background_on_timeout = (
        can_background
        and not (explicit and timeout_s <= SHORT_EXPLICIT_TIMEOUT_S)
        and not _waits_for_a_person(input.get("argv") or input.get("command"))
        and timeout["source"] != "deadline"
    )
    moved: str | None = None
    try:
        deadline = time.monotonic() + timeout_s
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if background_on_timeout:
                    run.to_background("timeout")
                else:
                    timed_out = True
                    _terminate(process, job)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=1)
                    break
            if run.requested.is_set():
                moved = registry.adopt(process, job, command, cwd or "", stdout, stderr, readers)
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=min(0.2, max(remaining, 0.01)))
    finally:
        if can_background:
            with _FOREGROUND_LOCK:
                _FOREGROUND.pop(key, None)
        if remove_cancel_callback is not None:
            remove_cancel_callback()
        cancelled = cancellation is not None and cancellation.cancelled
        if moved is None and process.poll() is None:
            _terminate(process, job)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
        if moved is None:
            for reader in readers:
                reader.join(timeout=1)
            if job is not None:
                job.close()

    if moved is not None:
        why = (
            "Moved to the background by the user"
            if run.reason != "timeout"
            else f"Still running after {timeout_s:.0f}s, so it moved to the background"
        )
        return _ok(
            {
                "command": command,
                "cwd": cwd,
                "background": True,
                "handle": moved,
                "reason": run.reason or "owner",
                "stdout": stdout.text()[-4000:],
                "stderr": stderr.text()[-4000:],
                "note": f"{why}: {moved}. Its output keeps arriving; read it with "
                "process.logs or wait with process.wait.",
            }
        )

    exit_code = process.returncode if process.returncode is not None else -1
    expected = input.get("expect_exit")
    expected = [expected] if isinstance(expected, int) else list(expected or [])
    data = {
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
        "timeout": timeout,
    }
    if expected and exit_code != 0 and exit_code in expected:
        # A red the model asked for on purpose is not a failed tool: it no
        # longer counts as an incident nor dirties the metrics (F4, F27).
        data["expected_exit"] = expected
        data["as_expected"] = True
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
                "On Windows `command` runs in cmd.exe unless `shell` says otherwise: chain "
                "with &&, there is no grep/head/tail; set shell=powershell or shell=bash to "
                "write in those syntaxes. Set a short explicit timeout for SSH and network "
                "probes, and an explicit longer timeout for builds/tests. To wait for a server, "
                "port, file or log line use wait.for, never sleep, ping, timeout or Start-Sleep."
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
                    "notify": {
                        "type": "string",
                        "enum": ["exit", "failure", "never", "wake"],
                        "description": (
                            "With background: when to be told it ended (default: failure); "
                            "wake also starts a new turn here when it ends after your turn."
                        ),
                    },
                    "cwd": {"type": "string"},
                    "timeout_s": {
                        "type": "number",
                        "minimum": 1,
                        "description": (
                            "Seconds; default 60, max 600. A longer-running command moves to "
                            "the background (process handle) unless timeout_s <= 30."
                        ),
                    },
                    "env": {"type": "object"},
                    "shell": SHELL_SCHEMA,
                    "expect_exit": {
                        "type": ["integer", "array"],
                        "items": {"type": "integer"},
                        "description": (
                            "Exit code(s) that mean the command did what you expected, e.g. 1 "
                            "for tests you expect to fail before a fix. Default 0. Some "
                            "programs report success with codes other than 0 (their "
                            "documentation lists them); pass those codes here so the run "
                            "is not counted as a failure."
                        ),
                    },
                },
                "oneOf": [{"required": ["command"]}, {"required": ["argv"]}],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=600_000,
            handler=shell_exec,
            normalize=normalize.shell_exec,
            namespace="shell",
            manifest={
                "notes": "executes with the session user; the policy engine decides allow/ask/deny"
            },
        ),
    ]
