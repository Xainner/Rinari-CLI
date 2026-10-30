"""Session-scoped process control: start, wait, output, signal, list.

Complements `shell.exec` (one-shot) with long-lived processes the agent can
observe and steer while they run. Handles live only inside one session; the
registry is per-ToolContext. Starting a process goes through the normal
Tool Runtime pipeline as `shell.exec`-class risk; managing an already-started
process is a low-risk follow-up of that approval (policy capability
`process.local`).

POSIX process groups give signal semantics; on Windows each process lives in
its own Job Object (plan 0.4.0 #90): stopping it ends the whole tree, and if
the Engine dies Windows ends it too. A conversation keeps at most
MAX_LIVE_PROCESSES running; the same command in the same folder is reused
instead of started twice; and what ended is told to the model once.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import subprocess
import sys
import threading
import time
from typing import Any

from rinari.tools import normalize
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

from .jobs import ProcessJob, popen_in_job
from .shell import SHELL_SCHEMA, _BoundedBuffer, _kill_tree, shell_argv

MAX_PROCESS_OUTPUT_BYTES = 512 * 1024
# The end of the output, for process.logs: a chatty watcher filled the first
# 512 KiB and every later error was invisible (#90).
TAIL_OUTPUT_BYTES = 512 * 1024
MAX_LIVE_PROCESSES = 8
DEFAULT_READY_TIMEOUT_S = 30.0
#: «wake» is «exit» that also starts a new turn here when the process ends
#: after the turn that started it (Ideas-2, 23-oct): launch, end the turn, be told.
NOTIFY_MODES = ("exit", "failure", "never", "wake")
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


class _ProcessBuffer(_BoundedBuffer):
    """Keep model cursors stable while the desktop observes recent output."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(max_bytes)
        self._tail = bytearray()
        self._lock = threading.RLock()
        self.tail_truncated = False

    def write(self, data: bytes) -> int:
        with self._lock:
            count = super().write(data)
            self._tail.extend(data)
            if len(self._tail) > TAIL_OUTPUT_BYTES:
                del self._tail[:-TAIL_OUTPUT_BYTES]
                self.tail_truncated = True
            return count

    def text(self) -> str:
        with self._lock:
            return super().text()

    def tail_text(self, max_bytes: int = 64000) -> str:
        """The last `max_bytes` of output (the desktop panel polls this; keep it small)."""
        with self._lock:
            return self._tail[-max_bytes:].decode("utf-8", errors="replace")


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
        "ended_at",
        "exit_code",
        "exit_noted",
        "id",
        "job",
        "name",
        "notify",
        "persist",
        "port",
        "process",
        "readers",
        "ready",
        "started_at",
        "stderr",
        "stdout",
        "stop_requested",
    )

    def __init__(self, handle_id: str, command: str, cwd: str, process: subprocess.Popen) -> None:
        self.id = handle_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.stdout = _ProcessBuffer(MAX_PROCESS_OUTPUT_BYTES)
        self.stderr = _ProcessBuffer(MAX_PROCESS_OUTPUT_BYTES)
        self.exit_code: int | None = None
        # First time the exit was observed (observation time, seconds
        # since the epoch like started_at). None while running.
        self.ended_at: float | None = None
        self.started_at = time.time()
        self.readers: list[threading.Thread] = []
        # Set when the handle is terminated through registry.kill.
        self.stop_requested = False
        # #90: a short name ("dev"), the port it serves, whether it is ready,
        # when to tell the model it ended, and whether it outlives the Engine.
        self.name: str | None = None
        self.port: int | None = None
        self.ready: bool | None = None
        self.notify = "failure"
        self.persist = False
        self.job: ProcessJob | None = None
        self.exit_noted = False


class ProcessRegistry:
    """Thread-safe registry of live session processes."""

    # Boot-scoped creation generation. Sequential handle ids restart in
    # every registry, so desktops scope destructive preconditions to
    # (engine_instance_id, registry generation, handle id).
    _generations = itertools.count(1)

    def __init__(
        self,
        base_env: dict[str, str] | None = None,
        *,
        session_id: str = "",
        ledger: Any | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, _Handle] = {}
        self._counter = 0
        self._base_env = dict(base_env or os.environ)
        self.generation = next(ProcessRegistry._generations)
        # #90: the durable list (engine-processes.sqlite) and whose they are.
        self.session_id = session_id
        self.ledger = ledger
        self._exits: list[_Handle] = []
        # Called (from any thread) when a process the model should hear about ends.
        self.on_exit: Any = None

    def live_count(self) -> int:
        return sum(1 for handle in self.list() if handle.exit_code is None)

    def find(self, name: str) -> _Handle | None:
        """The running process with this name, else the latest one that had it."""
        matches = [handle for handle in self.list() if handle.name == name]
        running = [handle for handle in matches if handle.exit_code is None]
        return (running or matches or [None])[-1]

    def running_same(self, command: Any, cwd: str) -> _Handle | None:
        for handle in self.list():
            if handle.exit_code is None and handle.command == command and handle.cwd == cwd:
                return handle
        return None

    def by_port(self, port: int) -> _Handle | None:
        for handle in self.list():
            if handle.exit_code is None and handle.port == port:
                return handle
        return None

    def pending_wakes(self) -> list[_Handle]:
        """Ended processes that asked to wake the conversation, not told yet."""
        with self._lock:
            return [handle for handle in self._exits if handle.notify == "wake"]

    def take_exit_notices(self) -> list[_Handle]:
        """Processes that ended since the last call and asked to be told (once)."""
        with self._lock:
            notices, self._exits = self._exits, []
        return notices

    def _observe_exit(self, handle: _Handle) -> None:
        if handle.exit_code is None or handle.exit_noted:
            return
        handle.exit_noted = True
        if self.ledger is not None:
            with contextlib.suppress(Exception):
                self.ledger.ended(self.session_id, handle.id)
        if handle.job is not None:
            handle.job.close()
        if handle.stop_requested or handle.notify == "never":
            return
        if handle.notify == "failure" and handle.exit_code == 0:
            return
        with self._lock:
            self._exits.append(handle)
        callback = self.on_exit
        if callable(callback):
            with contextlib.suppress(Exception):
                callback()

    def _register(self, handle: _Handle) -> None:
        if self.ledger is not None:
            with contextlib.suppress(Exception):
                self.ledger.record(self.session_id, handle)

    def adopt(
        self,
        process: subprocess.Popen,
        job: ProcessJob | None,
        command: Any,
        cwd: str,
        stdout: _ProcessBuffer,
        stderr: _ProcessBuffer,
        readers: list[threading.Thread],
    ) -> str:
        """Take a process that is already running (a foreground shell.exec, #32).

        Same PID, same pipes and readers: the output so far stays and keeps
        arriving in the same buffers.
        """
        with self._lock:
            self._counter += 1
            handle_id = f"proc_{self._counter:03d}"
            handle = _Handle(handle_id, command, cwd or "", process)
            handle.stdout, handle.stderr = stdout, stderr
            handle.readers = list(readers)
            handle.job = job
            self._handles[handle_id] = handle
        self._register(handle)
        threading.Thread(target=self._watch_exit, args=(handle,), daemon=True).start()
        return handle_id

    def _watch_exit(self, handle: _Handle) -> None:
        with contextlib.suppress(Exception):
            handle.process.wait()
        self.get(handle.id)

    def stop_all(self, *, include_persistent: bool = False) -> list[str]:
        stopped = []
        for handle in self.list():
            if handle.exit_code is None and (include_persistent or not handle.persist):
                self.kill(handle)
                stopped.append(handle.id)
        return stopped

    def start(
        self,
        command: str,
        cwd: str | None = None,
        env: dict | None = None,
        *,
        name: str | None = None,
        port: int | None = None,
        notify: str = "failure",
        persist: bool = False,
    ) -> str:
        kwargs: dict = {
            "shell": isinstance(command, str),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
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
        from rinari.tools.native.shell import resolve_argv

        # A persistent process is not killed with the Engine (no kill-on-close).
        process, job = popen_in_job(
            resolve_argv(command, process_env), kill_on_close=not persist, **kwargs
        )
        with self._lock:
            self._counter += 1
            handle_id = f"proc_{self._counter:03d}"
            handle = _Handle(handle_id, command, cwd or "", process)
            handle.job = job
            handle.name = name
            handle.port = port
            handle.notify = notify if notify in NOTIFY_MODES else "failure"
            handle.persist = persist
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
        self._register(handle)
        return handle_id

    def get(self, handle_id: str) -> _Handle | None:
        with self._lock:
            handle = self._handles.get(handle_id)
            if handle is not None:
                handle.exit_code = handle.process.poll()
                if handle.exit_code is not None and handle.ended_at is None:
                    handle.ended_at = time.time()
        if handle is not None:
            self._observe_exit(handle)
        return handle

    def list(self) -> list[_Handle]:
        with self._lock:
            handles = list(self._handles.values())
            for handle in handles:
                handle.exit_code = handle.process.poll()
                if handle.exit_code is not None and handle.ended_at is None:
                    handle.ended_at = time.time()
        for handle in handles:
            self._observe_exit(handle)
        return handles

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
        if handle.ended_at is None:
            handle.ended_at = time.time()
        for reader in handle.readers:
            with contextlib.suppress(Exception):
                reader.join(timeout=5)
        self._observe_exit(handle)

    def signal(self, handle: _Handle, name: str) -> bool:
        import signal as signal_module

        normalized = name.strip().upper()
        if sys.platform == "win32":
            if normalized in ("INT", "SIGINT", "TERM", "SIGTERM", "KILL", "SIGKILL"):
                self._terminate(handle)
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
        # A handle that already ended is never killed: its PID may belong to
        # another process by now.
        if handle.process.poll() is not None:
            self.get(handle.id)
            return
        handle.stop_requested = True
        if sys.platform == "win32" and _interrupt(handle.process, grace_s=2.0):
            # It ran its own shutdown. What is left of its tree (children of
            # npm, say) ends through its job object; never by PID, which may
            # belong to another process once this one is gone.
            if handle.job is not None:
                handle.job.terminate()
            return
        self._terminate(handle)

    def _terminate(self, handle: _Handle) -> None:
        if handle.job is not None and handle.job.terminate():
            return
        _kill_tree(handle.process)

    def _forget_dead_if_needed(self, handle: _Handle) -> None:
        """Keep handles until the agent reaps them; nothing to do."""


def _pump(stream, buffer, handle: _Handle, registry: ProcessRegistry) -> None:
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            buffer.write(chunk)
    except (OSError, ValueError):
        pass
    finally:
        # Stream EOF closely follows process exit; first observation wins.
        if handle.ended_at is None and handle.process.poll() is not None:
            handle.ended_at = time.time()
        if handle.process.poll() is not None:
            handle.exit_code = handle.process.returncode
            registry._observe_exit(handle)


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
    try:
        launched = shell_argv(command, input.get("shell"))
    except ValueError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    name = input.get("name")
    name = name.strip()[:40] if isinstance(name, str) and name.strip() else None
    ready_when = input.get("ready_when") if isinstance(input.get("ready_when"), dict) else None
    port = _port_of(ready_when)
    notify = input.get("notify") if input.get("notify") in NOTIFY_MODES else "failure"
    persist = input.get("persist") is True
    workdir = cwd or str(ctx.cwd)

    # The same server twice was the commonest leak (#90): reuse it.
    existing = registry.running_same(launched, workdir) or (
        registry.by_port(port) if port else None
    )
    if existing is not None:
        return _ok(
            {
                **_describe(existing),
                "reused": True,
                "note": "Already running; reused instead of starting it again.",
            }
        )
    if port and _listening(port):
        owner = _port_owner(port)
        return _fail(
            ToolErrorCode.CONFLICT,
            f"Port {port} is already in use"
            + (f" by {owner}" if owner else "")
            + "; it is not a process of this conversation. Stop it or use another port.",
        )
    if registry.live_count() >= MAX_LIVE_PROCESSES:
        running = ", ".join(_label(h) for h in registry.list() if h.exit_code is None)
        return _fail(
            ToolErrorCode.CONFLICT,
            f"This conversation already runs {MAX_LIVE_PROCESSES} processes ({running}); "
            "stop one with process.stop before starting another.",
        )
    handle_id = registry.start(
        launched, cwd=workdir, env=env, name=name, port=port, notify=notify, persist=persist
    )
    handle = registry.get(handle_id)
    assert handle is not None
    data = {**_describe(handle), "command": command}
    if ready_when:
        data.update(_wait_ready(handle, ready_when, ctx))
    return _ok(data)


def _port_of(ready_when: dict | None) -> int | None:
    if not ready_when:
        return None
    port = ready_when.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
        return port
    url = ready_when.get("url")
    if isinstance(url, str):
        from urllib.parse import urlparse

        with contextlib.suppress(ValueError):
            parsed = urlparse(url)
            if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                return parsed.port or (443 if parsed.scheme == "https" else 80)
    return None


def _listening(port: int) -> bool:
    import socket

    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.3):
        return True
    return False


def _port_owner(port: int) -> str | None:
    """Best effort «pid 1234 (node.exe)» for whoever listens on a local port."""
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if (
                len(parts) >= 5
                and parts[3].upper() == "LISTENING"
                and parts[1].endswith(f":{port}")
            ):
                pid = parts[4]
                image = (
                    subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    .stdout.split(",")[0]
                    .strip('"')
                )
                return f"pid {pid}" + (f" ({image})" if image else "")
    return None


def _wait_ready(handle: _Handle, ready_when: dict, ctx: ToolContext) -> dict:
    """Wait for the condition with wait.for; say ready or not, with the last lines."""
    from rinari.tools.native.wait import wait_for

    probe: dict = {"handle": handle.id}
    if ready_when.get("port") is not None:
        probe["port"] = ready_when["port"]
    elif ready_when.get("url"):
        probe["url"] = ready_when["url"]
    elif ready_when.get("line"):
        probe["output"] = ready_when["line"]
    else:
        return {"ready": None}
    timeout = ready_when.get("timeout_s", DEFAULT_READY_TIMEOUT_S)
    probe["timeout_s"] = timeout if isinstance(timeout, (int, float)) else DEFAULT_READY_TIMEOUT_S
    result = wait_for(probe, ctx)
    data = result.data if isinstance(result.data, dict) else {}
    handle.ready = bool(data.get("ready"))
    lines = _last_lines(handle, 8)
    return {"ready": handle.ready, "ready_state": data.get("state"), "last_lines": lines}


def _label(handle: _Handle) -> str:
    return f"{handle.id}{f' {handle.name}' if handle.name else ''}"


def _last_lines(handle: _Handle, count: int, pattern: str | None = None) -> list[str]:
    text = handle.stdout.tail_text(TAIL_OUTPUT_BYTES) + handle.stderr.tail_text(TAIL_OUTPUT_BYTES)
    lines = [line for line in text.splitlines() if line.strip()]
    if pattern:
        needle = pattern.lower()
        lines = [line for line in lines if needle in line.lower()]
    return [line[:400] for line in lines[-count:]]


def _interrupt(process, *, grace_s: float) -> bool:
    """Ask a Windows process group to stop as Ctrl+Break does, so a server
    runs its own shutdown, and wait a little (F19, plan 0.4.0 v2, V2-14).
    True when it ended; otherwise the caller ends it by force."""
    import signal

    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    except (OSError, ValueError, AttributeError):
        return False
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        return False
    return True


def _describe(handle: _Handle) -> dict:
    last = _last_lines(handle, 1)
    return {
        "handle": handle.id,
        "name": handle.name,
        "pid": handle.process.pid,
        "running": handle.exit_code is None,
        "exit_code": handle.exit_code,
        "port": handle.port,
        "ready": handle.ready,
        "persist": handle.persist,
        # Stopped because it was asked: its exit code (1 on Windows when
        # killed) is not a failure.
        **({"stop_requested": True} if getattr(handle, "stop_requested", False) else {}),
        "last_line": last[0] if last else None,
        "age_s": round(time.time() - handle.started_at, 1),
    }


def _lookup(ctx: ToolContext, input: dict) -> _Handle | None:
    """By handle, then by name, then by the port it serves."""
    registry = _registry(ctx)
    if registry is None:
        return None
    if isinstance(input.get("handle"), str) and input["handle"]:
        return registry.get(input["handle"])
    if isinstance(input.get("name"), str) and input["name"].strip():
        return registry.find(input["name"].strip())
    port = input.get("port")
    if isinstance(port, int) and not isinstance(port, bool):
        return registry.by_port(port)
    return None


def process_logs(input: dict, ctx: ToolContext) -> ToolResult:
    """The last lines of a process, optionally only those containing `grep` (#90)."""
    handle = _lookup(ctx, input)
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process: give handle or name")
    lines = input.get("lines", 50)
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 400:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "lines must be between 1 and 400")
    grep = input.get("grep") if isinstance(input.get("grep"), str) and input["grep"] else None
    registry = _registry(ctx)
    assert registry is not None
    registry.get(handle.id)
    return _ok(
        {
            **_describe(handle),
            "lines": _last_lines(handle, lines, grep),
            "older_dropped": handle.stdout.tail_truncated or handle.stderr.tail_truncated,
        }
    )


def process_stop(input: dict, ctx: ToolContext) -> ToolResult:
    """Stop a process and everything it started, by handle, name or port (#90)."""
    handle = _lookup(ctx, input)
    port = input.get("port")
    if handle is None:
        if isinstance(port, int) and not isinstance(port, bool) and _listening(port):
            owner = _port_owner(port)
            return _fail(
                ToolErrorCode.CONFLICT,
                f"Port {port} is served by a process this conversation did not start"
                + (f" ({owner})" if owner else "")
                + "; it is not stopped from here.",
            )
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process: give handle, name or port")
    registry = _registry(ctx)
    assert registry is not None
    if handle.exit_code is None:
        registry.kill(handle)
        registry.wait(handle, 5)
    return _ok({**_describe(registry.get(handle.id) or handle), "stopped": True})


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
    from rinari.tools.native.shell import effective_timeout

    requested = input.get("timeout_s")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        requested = min(max(float(requested), 0.0), MAX_WAIT_TIMEOUT_S)
    timeout = effective_timeout(requested, DEFAULT_WAIT_TIMEOUT_S, ctx)
    exited = registry.wait(handle, timeout["effective_s"])
    if not exited:
        # The process keeps running; only this wait ended.
        return _ok({"handle": handle.id, "exit_code": None, "timed_out": True, "timeout": timeout})
    return _ok({"handle": handle.id, "exit_code": handle.exit_code, "timed_out": False})


def process_output(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve_handle(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")
    stdout, stderr = handle.stdout.text(), handle.stderr.text()
    cursor = input.get("cursor") or {}
    limit = input.get("max_chars", 16000)
    if not isinstance(cursor, dict) or not isinstance(limit, int) or not 1 <= limit <= 64000:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Invalid cursor or max_chars")
    offsets = [cursor.get("stdout", 0), cursor.get("stderr", 0)]
    if any(not isinstance(n, int) or n < 0 for n in offsets):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Cursor offsets must be nonnegative integers")
    if offsets[0] > len(stdout) or offsets[1] > len(stderr):
        return _fail(ToolErrorCode.CONFLICT, "Cursor does not belong to this output snapshot")
    out = stdout[offsets[0] : offsets[0] + limit]
    err = stderr[offsets[1] : offsets[1] + limit]
    return _ok(
        {
            "handle": handle.id,
            "running": handle.exit_code is None,
            "exit_code": handle.exit_code,
            "stdout": out,
            "stderr": err,
            "cursor": {"stdout": offsets[0] + len(out), "stderr": offsets[1] + len(err)},
            "has_more": offsets[0] + len(out) < len(stdout) or offsets[1] + len(err) < len(stderr),
            "truncated": handle.stdout.truncated or handle.stderr.truncated,
        }
    )


def process_signal(input: dict, ctx: ToolContext) -> ToolResult:
    handle = _resolve_handle(ctx, input.get("handle"))
    if handle is None:
        return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")
    if handle.exit_code is not None:
        return _ok({"handle": handle.id, "state": "exited", "exit_code": handle.exit_code})
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
    return _ok(
        {
            "handle": handle.id,
            "signal": name.strip().upper(),
            "state": "exited" if handle.process.poll() is not None else "signal_requested",
        }
    )


def process_list(input: dict, ctx: ToolContext) -> ToolResult:
    registry = _registry(ctx)
    if registry is None:
        return _ok({"processes": []})
    entries = []
    for handle in registry.list():
        if handle.process.poll() is not None:
            handle.exit_code = handle.process.returncode
        if input.get("running_only") and handle.exit_code is not None:
            continue
        entries.append({**_describe(handle), "command": handle.command, "cwd": handle.cwd})
    return _ok({"processes": entries})


# -- tool definitions ---------------------------------------------------------


def _classify_shell_like(input: dict) -> ClassifiedAction:
    from rinari.tools.definition import command_cwd, command_text

    return ClassifiedAction("shell.exec", command_text(input), cwd=command_cwd(input))


def _classify_process_local(input: dict) -> ClassifiedAction:
    return ClassifiedAction("process.local", None)


def process_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="process.start",
            description=(
                "Start a server or watcher; returns a handle. ready_when waits for a port, "
                "url or line. A same command already running is reused. Max 8; stopped "
                "when Rinari closes unless persist (the user is asked)."
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
                    "cwd": {"type": "string"},
                    "env": {"type": "object"},
                    "shell": SHELL_SCHEMA,
                    "name": {"type": "string"},
                    "ready_when": {
                        "type": "object",
                        "properties": {
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "url": {"type": "string"},
                            "line": {"type": "string"},
                            "timeout_s": {"type": "number", "minimum": 1, "maximum": 300},
                        },
                    },
                    "notify": {
                        "type": "string",
                        "enum": list(NOTIFY_MODES),
                        "description": (
                            "When to be told it ended (default: failure). wake: also start a "
                            "new turn here when it ends after your turn did, so you can end "
                            "your turn instead of waiting for long work."
                        ),
                    },
                    "persist": {"type": "boolean"},
                },
                "oneOf": [{"required": ["command"]}, {"required": ["argv"]}],
            },
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=330_000,
            handler=process_start,
            normalize=normalize.process_start,
            classify=_classify_shell_like,
            namespace="process",
        ),
        ToolDefinition(
            name="process.wait",
            description=(
                "Wait up to timeout_s seconds (default 60) for a started process to exit; "
                "timed_out=true means it still runs."
            ),
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
            normalize=normalize.process_wait,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.output",
            description=(
                "Read output produced so far, without waiting. Pass the returned cursor to "
                "receive only new output."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "cursor": {
                        "type": "object",
                        "properties": {
                            "stdout": {"type": "integer", "minimum": 0},
                            "stderr": {"type": "integer", "minimum": 0},
                        },
                    },
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 64000},
                },
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
            name="process.logs",
            description=("Last lines of a process (handle or name); grep keeps matching lines."),
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "name": {"type": "string"},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 400},
                    "grep": {"type": "string"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=process_logs,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.stop",
            description=("Stop a process and its children, by handle, name or port."),
            input_schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "name": {"type": "string"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=True,
            timeout_ms=30_000,
            handler=process_stop,
            classify=_classify_process_local,
            namespace="process",
        ),
        ToolDefinition(
            name="process.list",
            description=(
                "List processes started in this conversation: name, port, ready, last line, age."
            ),
            input_schema={"type": "object", "properties": {"running_only": {"type": "boolean"}}},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=process_list,
            classify=_classify_process_local,
            namespace="process",
        ),
    ]


def processes_note(registry: ProcessRegistry | None, ended: list[_Handle]) -> str | None:
    """What the model has running and what ended, for the turn context (#90).

    It did not know at the start of a turn what it had left running, so it
    started the same server again; nor that one had died.
    """
    if registry is None:
        return None
    running = [handle for handle in registry.list() if handle.exit_code is None]
    lines: list[str] = []
    if running:
        lines.append("Processes running in this conversation (process.logs / process.stop):")
        for handle in running:
            parts = [handle.id]
            if handle.name:
                parts.append(f"«{handle.name}»")
            parts.append(f"`{_command_text(handle.command)[:80]}`")
            if handle.port:
                parts.append(f":{handle.port}")
            if handle.ready is not None:
                parts.append("ready" if handle.ready else "not ready")
            parts.append(f"{int((time.time() - handle.started_at) // 60)} min")
            lines.append("- " + " · ".join(parts))
    if ended:
        lines.append("Processes that ended since you last looked:")
        for handle in ended:
            last = _last_lines(handle, 1)
            tail = f" — last line: {last[0][:200]}" if last else ""
            name = f" «{handle.name}»" if handle.name else ""
            lines.append(f"- {handle.id}{name} exited with code {handle.exit_code}{tail}")
    return "\n".join(lines) if lines else None


def _command_text(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)
