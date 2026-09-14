"""Shared HTTP helpers for provider adapters.

Transport/timeout failures become NetworkError (exit 10). Status-code
interpretation (auth vs. provider errors) belongs to each adapter.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager

import httpx

from rinari.shared.errors import NetworkError, ProviderModelError

# Model generation can run for minutes; the client default (10s) only fits
# discovery/health probes.
MODEL_CALL_TIMEOUT = 600.0
# Streaming calls need a much shorter inactivity bound. A healthy long-running
# response may keep streaming for minutes, but waiting ten minutes for the first
# byte makes a dead provider look like a permanently thinking model.
MODEL_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=10.0)
DEFAULT_MODEL_STREAM_READ_TIMEOUT_S = 30.0
MIN_MODEL_STREAM_READ_TIMEOUT_S = 1.0
MAX_MODEL_STREAM_READ_TIMEOUT_S = 600.0

OPENCODE_SESSION_HEADER = "x-opencode-session"

_TOOL_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

#: Official wire contract for function tool names, shared by OpenAI
#: (max 64) and Anthropic (max 128): letters, digits, underscore, hyphen.
#: The 64 bound is the stricter of the two and what the router uses to
#: decide when the reversible alias map is needed (providers/PLAN F3).
WIRE_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _is_opencode_host(url: str | None) -> bool:
    if not url:
        return False
    host = httpx.URL(url).host or ""
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def session_affinity_headers(url: str | None, session_id: str | None) -> dict[str, str]:
    """Vendor session-affinity headers for model calls.

    OpenCode requires a stable per-conversation id on its own endpoints
    (enforced 2026-09-06); requests without it may error. Only sent to
    the vendor's own hosts and only when the caller propagated a session
    id. The value is Rinari's opaque session id (no secret material).
    """
    if not session_id or not _is_opencode_host(url):
        return {}
    return {OPENCODE_SESSION_HEADER: session_id}


def sanitize_tool_name(name: str) -> str:
    """Rewrite one tool name to the strict function-name pattern.

    Strict OpenAI-compatible vendors (e.g. OpenCode Go) reject names
    outside ^[a-zA-Z0-9_-]+$; Rinari's dotted names (fs.read) fall in
    that bucket. Fellow of needs_tool_aliasing: only applied where the
    vendor requires it, and mapped back on the way in.
    """
    return _TOOL_NAME_UNSAFE.sub("_", name)


def is_opencode_endpoint(url: str | None) -> bool:
    """Whether this URL belongs to the vendor's own API hosts."""
    return _is_opencode_host(url)


def needs_tool_aliasing(endpoint: str | None) -> bool:
    """Deprecated host check; the router now aliases per tool-name pattern.

    Kept for the OpenCode session-affinity code path only; tool-name
    aliasing is decided by WIRE_TOOL_NAME_RE in the router (F3).
    """
    return _is_opencode_host(endpoint)


def send_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    try:
        return client.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except httpx.TimeoutException as exc:
        timeout_s = timeout if isinstance(timeout, (int, float)) else None
        raise NetworkError(
            f"Timed out contacting {url}",
            details={
                "kind": "TIMEOUT",
                "phase": "request",
                "timeout_s": timeout_s,
            },
        ) from exc
    except httpx.TransportError as exc:
        raise NetworkError(f"Cannot reach {url}: {exc.__class__.__name__}") from exc


def model_stream_timeout(read_timeout_s: float | None = None) -> httpx.Timeout:
    """Build the bounded streaming timeout used by model adapters."""
    read = DEFAULT_MODEL_STREAM_READ_TIMEOUT_S if read_timeout_s is None else read_timeout_s
    return httpx.Timeout(connect=15.0, read=read, write=30.0, pool=10.0)


def stream_timeout_error(
    url: str,
    model: str,
    exc: httpx.TimeoutException,
    *,
    timeout_s: float,
    headers_received: bool,
    saw_payload: bool,
    stream_started_at: float,
    last_activity_at: float,
    effective_timeouts: dict | None = None,
    response: httpx.Response | None = None,
) -> NetworkError:
    """Normalize a stream timeout without losing where the wait occurred."""
    if isinstance(exc, httpx.ConnectTimeout):
        phase = "connect"
        effective_timeout_s = (effective_timeouts or {}).get("connect", 15.0)
    elif isinstance(exc, httpx.PoolTimeout):
        phase = "pool"
        effective_timeout_s = 10.0
    elif isinstance(exc, httpx.WriteTimeout):
        phase = "write"
        effective_timeout_s = 30.0
    elif isinstance(exc, httpx.ReadTimeout) and not headers_received:
        phase = "response_headers"
        effective_timeout_s = timeout_s
    elif isinstance(exc, httpx.ReadTimeout) and not saw_payload:
        phase = "first_byte"
        effective_timeout_s = timeout_s
    elif isinstance(exc, httpx.ReadTimeout):
        phase = "between_chunks"
        effective_timeout_s = timeout_s
    else:
        phase = "read"
        effective_timeout_s = timeout_s
    if effective_timeouts and phase in {"first_byte", "response_headers", "between_chunks"}:
        effective_timeout_s = effective_timeouts[
            "idle" if phase == "between_chunks" else "first_byte"
        ]
    now = time.monotonic()
    last_payload_at_s = round(last_activity_at - stream_started_at, 3) if saw_payload else None
    return NetworkError(
        f"Timed out streaming from {url}",
        details={
            "kind": "TIMEOUT",
            "phase": phase,
            "timeout_s": effective_timeout_s,
            # These measure complete provider payload lines, not raw socket
            # bytes, because httpx exposes the stream to us at line boundaries.
            "last_payload_at_s": last_payload_at_s,
            "payload_idle_s": round(now - last_activity_at, 3),
            "headers_received": headers_received,
            "saw_payload": saw_payload,
            "partial": saw_payload,
            "model": model,
            "effective_timeouts": effective_timeouts,
            "request_id": response.headers.get("x-request-id") if response is not None else None,
        },
    )


def provider_error_detail(response: httpx.Response, url: str) -> str:
    """Best-effort human detail from an error body (no secret leakage)."""
    # A streaming response body is not loaded until read(); without this the
    # error path itself crashes with httpx.ResponseNotRead, masking the real
    # provider error (openai/anthropic invoke_stream pass unread responses).
    try:
        if not response.is_stream_consumed:
            response.read()
    except Exception:
        return f"Provider returned HTTP {response.status_code} for {url}"
    try:
        data = response.json()
    except ValueError:
        return f"Provider returned HTTP {response.status_code} for {url}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return f"Provider returned HTTP {response.status_code}: {message[:300]}"
    return f"Provider returned HTTP {response.status_code} for {url}"


def auth_failure(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderModelError:
    from rinari.providers.errors import ProviderError, ProviderErrorCode

    return ProviderError(
        f"Authentication failed (HTTP {response.status_code}) for {url}",
        code=ProviderErrorCode.AUTH,
        retryable=False,
        provider=provider,
        model=model,
        hint="Check the provider credential: `rinari providers auth <alias>`.",
    )


def provider_error(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderModelError:
    from rinari.providers.errors import classify_http_error

    return classify_http_error(response, url, provider=provider, model=model)


def decode_json(response: httpx.Response, url: str):
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderModelError(f"Provider returned invalid JSON for {url}") from exc


STREAM_DEFAULTS = {"connect": 15.0, "first_byte": 120.0, "idle": 120.0, "total": 900.0}


def validate_stream_timeouts(value):
    import math

    if not isinstance(value, dict) or set(value) - set(STREAM_DEFAULTS):
        raise ValueError("Stream timeouts accept connect, first_byte, idle and total")
    limits = {**STREAM_DEFAULTS, **value}
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
        for v in limits.values()
    ):
        raise ValueError("Stream timeouts must be finite positive seconds")
    return limits


def stream_socket_timeout(request):
    limits = validate_stream_timeouts(request.stream_timeouts) if request.stream_timeouts else None
    if not limits:
        return model_stream_timeout(request.stream_read_timeout_s)
    return httpx.Timeout(
        connect=limits["connect"],
        read=max(limits["first_byte"], limits["idle"]),
        write=30.0,
        pool=10.0,
    )


@contextmanager
def open_model_stream(client, request, started_at, *args, **kwargs):
    """Cancel/bound the response-header wait without transferring runtime state to a worker.

    An abandoned opener closes its own response as soon as the transport returns.
    No automatic retry is performed: the remote request may already have started.
    """
    import threading

    limits = (
        validate_stream_timeouts(request.stream_timeouts)
        if request.stream_timeouts
        else {
            **STREAM_DEFAULTS,
            "first_byte": request.stream_read_timeout_s or 30.0,
        }
    )
    ready = threading.Event()
    guard = threading.Lock()
    state = {"abandoned": False}
    context = client.stream(*args, **kwargs, timeout=stream_socket_timeout(request))

    def open_response():
        try:
            response = context.__enter__()
        except BaseException as exc:
            with guard:
                state["error"] = exc
                ready.set()
            return
        with guard:
            abandoned = state["abandoned"]
            if not abandoned:
                state["response"] = response
            ready.set()
        if abandoned:
            context.__exit__(None, None, None)

    threading.Thread(target=open_response, daemon=True, name="provider-stream-open").start()
    try:
        while not ready.wait(0.05):
            if request.cancellation:
                request.cancellation.throw_if_cancelled()
            elapsed = time.monotonic() - started_at
            phase = "total" if limits["total"] <= limits["first_byte"] else "response_headers"
            bound = min(limits["first_byte"], limits["total"])
            if elapsed >= bound:
                raise NetworkError(
                    "Timed out waiting for provider response headers",
                    details={
                        "kind": "TIMEOUT",
                        "phase": phase,
                        "timeout_s": bound,
                        "elapsed_s": round(elapsed, 3),
                        "bytes_received": 0,
                        "effective_timeouts": limits,
                    },
                )
        if request.cancellation:
            request.cancellation.throw_if_cancelled()
        if "error" in state:
            raise state["error"]
        try:
            yield state["response"]
        except (NetworkError, ProviderModelError) as exc:
            exc.details.setdefault("request_id", state["response"].headers.get("x-request-id"))
            exc.details.setdefault("elapsed_s", round(time.monotonic() - started_at, 3))
            exc.details.setdefault("model", request.model)
            exc.details.setdefault("effective_timeouts", limits)
            raise
    finally:
        with guard:
            state["abandoned"] = True
            opened = "response" in state
        if opened:
            context.__exit__(None, None, None)


def iter_model_lines(response, request, started_at):
    """Bound silence/total wait independently; bytes and heartbeats are not reasoning progress.

    A reader touches only the HTTP response, never application/session storage.
    The consumer owns parsing and cancellation; no partial tool call escapes it.
    """
    import codecs
    import queue
    import threading

    limits = (
        validate_stream_timeouts(request.stream_timeouts)
        if request.stream_timeouts
        else validate_stream_timeouts(
            {
                "first_byte": request.stream_read_timeout_s or 30.0,
                "idle": request.stream_read_timeout_s or 30.0,
            }
        )
    )
    channel = queue.Queue(maxsize=64)
    stopped = threading.Event()
    received = {"bytes": 0, "last": started_at}

    def put(value):
        while not stopped.is_set():
            try:
                channel.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def read():
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        try:
            for chunk in response.iter_bytes():
                if stopped.is_set():
                    return
                received["bytes"] += len(chunk)
                received["last"] = time.monotonic()
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    put(("line", line.rstrip("\r")))
                if len(buffer) > 16 * 1024 * 1024:
                    raise NetworkError("Provider SSE line exceeds transport buffer")
            buffer += decoder.decode(b"", final=True)
            if buffer:
                put(("line", buffer.rstrip("\r")))
            put(("done", None))
        except BaseException as exc:
            put(("error", exc))

    threading.Thread(target=read, daemon=True, name="provider-stream-reader").start()
    try:
        while True:
            if request.cancellation:
                request.cancellation.throw_if_cancelled()
            now = time.monotonic()
            phase = "between_chunks" if received["bytes"] else "first_byte"
            limit = limits["idle"] if received["bytes"] else limits["first_byte"]
            if now - started_at >= limits["total"]:
                phase, limit = "total", limits["total"]
            elif not channel.empty() or now - received["last"] < limit:
                phase = None
            if phase:
                raise NetworkError(
                    "Timed out streaming from provider",
                    details={
                        "kind": "TIMEOUT",
                        "phase": phase,
                        "timeout_s": limit,
                        "elapsed_s": round(now - started_at, 3),
                        "bytes_received": received["bytes"],
                        "partial": False,
                        "request_id": response.headers.get("x-request-id"),
                        "effective_timeouts": limits,
                    },
                )
            try:
                kind, value = channel.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "done":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        stopped.set()
        response.close()
