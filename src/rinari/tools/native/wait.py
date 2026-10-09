"""wait.for: wait until a server, a process's output or a file is ready.

An audit of real sessions found models waiting for servers with blind sleeps
through the shell (`ping -n 6 127.0.0.1`, `Start-Sleep`, `timeout /t`, `sleep`):
73 calls and about 24 minutes of waiting that was too short when the server was
slow and wasted when it was fast. wait.for polls one concrete condition and
returns as soon as it holds, when its timeout ends, when the turn is cancelled,
or when the process it watches exits, with what it last observed.

Policy is never weaker than the tool that would otherwise do the check: a port
or URL is a `network.outbound` read like an http.request GET (local hosts are
allowed by the network policy, internet hosts follow network.mode and mark the
turn as having read outside content), a file is an `fs.read`, and watching a
handle is `process.local` like process.output.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

from rinari.tools import normalize
from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 600.0
DEFAULT_INTERVAL_S = 0.5
MIN_INTERVAL_S = 0.1
MAX_INTERVAL_S = 10.0
# Any HTTP answer below 500 means the server is up: an API without a root
# route answers 404, while 502/503 come from a proxy or a server still booting.
DEFAULT_STATUS_MIN = 200
DEFAULT_STATUS_MAX = 499
# One probe never blocks longer than this, so a cancel lands within seconds.
_PROBE_CAP_S = 3.0
_TAIL_CHARS = 800
_LINE_CHARS = 300

CONDITIONS = ("port", "url", "output", "file")
_ONLY_WITH = {
    "host": "port",
    "status_min": "url",
    "status_max": "url",
    "regex": "output",
}


def _ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def _conditions(args: dict) -> list[str]:
    return [name for name in CONDITIONS if args.get(name) is not None]


def _invalid(args: dict) -> str | None:
    """Why the arguments do not describe exactly one condition, or None."""
    given = _conditions(args)
    if len(given) != 1:
        return (
            "Give exactly one condition: port (with optional host), url, output "
            "(text in a process handle's output) or file"
            + (f"; received {', '.join(given)}" if given else "")
        )
    kind = given[0]
    for key, owner in _ONLY_WITH.items():
        if args.get(key) not in (None, False) and owner != kind:
            return f"{key} only applies to the {owner} condition"
    if kind == "output" and not isinstance(args.get("handle"), str):
        return "output needs handle: the process handle from process.start or shell.exec"
    if kind == "output":
        pattern = args.get("output")
        if not isinstance(pattern, str) or not pattern:
            return "output must be non-empty text"
        if args.get("regex"):
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"output is not a valid regex: {exc}"
    if kind == "url":
        from rinari.tools.native.web import _validate_url

        if _validate_url(args.get("url")) is None:
            return "url must be an http(s) URL"
        low = args.get("status_min", DEFAULT_STATUS_MIN)
        high = args.get("status_max", DEFAULT_STATUS_MAX)
        if not isinstance(low, int) or not isinstance(high, int) or low > high:
            return "status_min/status_max must be integers with status_min <= status_max"
    if kind == "file" and (not isinstance(args.get("file"), str) or not args["file"]):
        return "file must be a non-empty path"
    if kind == "port":
        host = args.get("host", "localhost")
        if not isinstance(host, str) or not host.strip():
            return "host must be a non-empty host name or IP address"
    return None


def _precheck(args: dict, ctx: ToolContext) -> ToolResult | None:
    # Rejected before policy: an approval for a call that cannot run is noise.
    problem = _invalid(args)
    return _fail(ToolErrorCode.INVALID_ARGUMENT, problem) if problem else None


def _classify_many(args: dict) -> list[ClassifiedAction]:
    """Every resource the call touches; none of them decides alone."""
    actions: list[ClassifiedAction] = []
    if args.get("port") is not None:
        host = str(args.get("host") or "localhost").strip()
        actions.append(ClassifiedAction("network.outbound", f"{host}:{args['port']}", "read"))
    if args.get("url") is not None:
        actions.append(ClassifiedAction("network.outbound", str(args["url"]), "read"))
    if args.get("file") is not None:
        actions.append(ClassifiedAction("fs.read", str(args["file"])))
    if args.get("handle") is not None or args.get("output") is not None or not actions:
        actions.append(ClassifiedAction("process.local", None))
    return actions


def _classify(args: dict) -> ClassifiedAction:
    return _classify_many(args)[0]


# -- probes ---------------------------------------------------------------------


def _port_probe(host: str, port: int):
    def probe(budget: float) -> tuple[bool, dict]:
        try:
            with socket.create_connection((host, port), timeout=max(0.2, budget)):
                return True, {"connected": True}
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc) or ""
            return False, {"error": f"{type(exc).__name__}: {reason}".rstrip(": ")}

    return probe


def _url_probe(url: str, low: int, high: int, ctx: ToolContext):
    from rinari.http.client import request
    from rinari.tools.native.web import _client_factory
    from rinari.web.client import WebRequestError

    token = ctx.cancellation
    is_cancelled = (lambda: bool(getattr(token, "cancelled", False))) if token else None

    def probe(budget: float) -> tuple[bool, dict]:
        try:
            # No redirects: a 3xx already proves the server answers, and
            # following it could reach a host the policy never looked at.
            response = request(
                url,
                method="GET",
                follow_redirects=False,
                timeout_s=budget,
                max_bytes=4096,
                client_factory=_client_factory(ctx),
                is_cancelled=is_cancelled,
            )
        except WebRequestError as exc:
            return False, {"error": f"{exc.code}: {exc.message}"}
        return low <= response.status <= high, {"status": response.status}

    return probe


def _file_probe(path: Path):
    def probe(budget: float) -> tuple[bool, dict]:
        try:
            stat = path.stat()
        except OSError:
            return False, {"exists": False}
        return True, {"exists": True, "size_bytes": stat.st_size}

    return probe


def _output_text(handle) -> str:
    # The head buffer stops growing at its cap; the tail keeps the newest
    # output, so a line printed late in a chatty log is still found.
    parts = []
    for buffer in (handle.stdout, handle.stderr):
        head = buffer.text()
        parts.append(head)
        if buffer.truncated:
            parts.append(buffer.tail_text())
    return "\n".join(parts)


def _output_probe(handle, pattern: str, regex: bool):
    compiled = re.compile(pattern) if regex else None

    def probe(budget: float) -> tuple[bool, dict]:
        text = _output_text(handle)
        if compiled is not None:
            match = compiled.search(text)
            position = match.start() if match else -1
        else:
            position = text.find(pattern)
        if position < 0:
            return False, {"matched": False}
        start = text.rfind("\n", 0, position) + 1
        end = text.find("\n", position)
        line = text[start : end if end >= 0 else len(text)].rstrip("\r")
        return True, {"matched": True, "line": line[:_LINE_CHARS]}

    return probe


def _tail(handle) -> str:
    text = handle.stdout.tail_text() + handle.stderr.tail_text()
    return text[-_TAIL_CHARS:]


# -- handler --------------------------------------------------------------------


def wait_for(input: dict, ctx: ToolContext) -> ToolResult:
    problem = _invalid(input)
    if problem:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, problem)
    kind = _conditions(input)[0]

    watched = None
    if input.get("handle") is not None:
        from rinari.tools.native.process import _registry, _resolve_handle

        if _registry(ctx) is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, "process registry unavailable")
        watched = _resolve_handle(ctx, input.get("handle"))
        if watched is None:
            return _fail(ToolErrorCode.NOT_FOUND, "unknown process handle")

    if kind == "port":
        host = str(input.get("host") or "localhost").strip()
        port = int(input["port"])
        target = f"{host}:{port}"
        denied = _guard(ctx, target)
        if denied is not None:
            return denied
        probe = _port_probe(host, port)
    elif kind == "url":
        target = str(input["url"]).strip()
        denied = _guard(ctx, target)
        if denied is not None:
            return denied
        probe = _url_probe(
            target,
            int(input.get("status_min", DEFAULT_STATUS_MIN)),
            int(input.get("status_max", DEFAULT_STATUS_MAX)),
            ctx,
        )
    elif kind == "file":
        try:
            resolved = ctx.sandbox.resolve(input["file"], base=ctx.cwd)
            ctx.sandbox.assert_readable(resolved)
        except Exception as exc:  # SandboxViolationError
            return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
        target = str(resolved)
        probe = _file_probe(resolved)
    else:
        target = f"{watched.id}: {input['output']}"
        probe = _output_probe(watched, input["output"], bool(input.get("regex")))

    from rinari.tools.native.shell import effective_timeout

    requested = input.get("timeout_s")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        requested = min(max(float(requested), 0.0), MAX_TIMEOUT_S)
    timeout = effective_timeout(requested, DEFAULT_TIMEOUT_S, ctx)
    interval = input.get("interval_s", DEFAULT_INTERVAL_S)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool):
        interval = DEFAULT_INTERVAL_S
    interval = min(max(float(interval), MIN_INTERVAL_S), MAX_INTERVAL_S)

    started = time.monotonic()
    state, checks, last, process = _poll(ctx, probe, watched, timeout["effective_s"], interval)
    data: dict[str, Any] = {
        "condition": kind,
        "target": target,
        "ready": state == "ready",
        "state": state,
        "elapsed_s": round(time.monotonic() - started, 1),
        "checks": checks,
        "last": last,
    }
    if process is not None:
        data["process"] = process
    if state == "timed_out":
        data["timeout"] = timeout
    return _ok(data)


def _guard(ctx: ToolContext, target: str) -> ToolResult | None:
    guard = ctx.network
    if guard is None:
        return None
    try:
        guard.assert_reachable(target, source="wait")
    except Exception as exc:  # SandboxViolationError
        return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    return None


def _poll(ctx: ToolContext, probe, watched, timeout_s: float, interval: float):
    """Probe until ready, timeout, cancellation or the watched process exits."""
    token = ctx.cancellation
    wake = threading.Event()
    # The turn's cancel wakes the wait at once instead of after the interval.
    unregister = token.on_cancel(wake.set) if hasattr(token, "on_cancel") else None
    deadline = time.monotonic() + timeout_s
    checks = 0
    last: dict = {}
    process = None
    try:
        while True:
            if token is not None:
                token.throw_if_cancelled()
            remaining = deadline - time.monotonic()
            checks += 1
            ready, last = probe(min(_PROBE_CAP_S, max(remaining, 0.2)))
            if ready:
                return "ready", checks, last, process
            if watched is not None and watched.process.poll() is not None:
                from rinari.tools.native.process import _registry

                registry = _registry(ctx)
                if registry is not None:
                    # Joins the output readers: the last lines it printed are
                    # what explains the exit, and may hold the awaited text.
                    registry.wait(watched, 0)
                ready, last = probe(0.2)
                process = {
                    "handle": watched.id,
                    "exit_code": watched.exit_code,
                    "output_tail": _tail(watched),
                }
                return ("ready" if ready else "exited"), checks, last, process
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if watched is not None:
                    process = {"handle": watched.id, "running": True}
                    if "matched" in last:
                        process["output_tail"] = _tail(watched)
                return "timed_out", checks, last, process
            wake.wait(min(interval, remaining))
    finally:
        if unregister is not None:
            unregister()


def wait_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="wait.for",
            description=(
                "Wait until a condition holds instead of sleeping (never sleep, ping, "
                "timeout or Start-Sleep through the shell). Exactly one condition: port "
                "(TCP accepts, host defaults to localhost), url (answers with a status in "
                "status_min..status_max, default 200..499), output (text, or a regex with "
                "regex=true, appears in a process handle's output) or file (exists). "
                "handle also stops the wait if that process exits. Returns state "
                "ready/timed_out/exited, elapsed_s and the last observation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "host": {"type": "string", "description": "For port; default localhost."},
                    "url": {"type": "string"},
                    "status_min": {"type": "integer", "minimum": 100, "maximum": 599},
                    "status_max": {"type": "integer", "minimum": 100, "maximum": 599},
                    "output": {"type": "string", "minLength": 1},
                    "regex": {"type": "boolean"},
                    "handle": {
                        "type": "string",
                        "description": "Process handle (process.start or shell.exec background).",
                    },
                    "file": {"type": "string"},
                    "timeout_s": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Seconds; default 60, max 600.",
                    },
                    "interval_s": {
                        "type": "number",
                        "minimum": MIN_INTERVAL_S,
                        "maximum": MAX_INTERVAL_S,
                    },
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            # Room for the longest wait plus one probe; the handler's own
            # timeout is what normally ends it.
            timeout_ms=int((MAX_TIMEOUT_S + 2 * _PROBE_CAP_S) * 1000),
            handler=wait_for,
            precheck=_precheck,
            normalize=normalize.process_wait,
            classify=_classify,
            classify_many=_classify_many,
            namespace="wait",
        )
    ]
