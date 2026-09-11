"""Native HTTP tools (tools.md, phase 5): generic requests and SSE.

Contract: a completed exchange is reported as data even when the status is
4xx/5xx (the tool's job is to make the request and return the response);
only transport-level failures (timeout, connect) map to tool error codes.
Auth arrives exclusively through secret references (env://, file://)
resolved by the session CredentialStore; secrets are redacted from every
result. Bodies over the preview limit or binary responses become artifacts
in the session artifact directory.
"""

from __future__ import annotations

import time
from typing import Any

from rinari.http.auth import apply_auth
from rinari.http.client import (
    ALLOWED_METHODS,
    HttpResponse,
    request,
    sse_lines,
)
from rinari.http.sse import parse_sse_events
from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
)
from rinari.tools.native.web import _client_factory, _fail, _guard, _ok, _safe_name, _validate_url
from rinari.web.client import MAX_RESPONSE_BYTES, WebRequestError

MAX_BODY_PREVIEW_CHARS = 8_000
MAX_BODY_PRETTY_CHARS = 1_024
MAX_SSE_EVENTS_DEFAULT = 50
MAX_SSE_EVENTS_LIMIT = 500
MAX_SSE_DATA_CHARS = 16_000


def _tool_err(exc: WebRequestError) -> ToolResult:
    return _fail(ToolErrorCode(exc.code), exc.message, retryable=exc.retryable)


def _headers_input(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            return None
        out[key] = val
    return out


def _query_input(value: Any) -> dict[str, str] | None:
    return _headers_input(value)


def _artifact_path(ctx: ToolContext, name: str, body: bytes) -> str:
    from pathlib import Path

    root = Path(ctx.artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    target.write_bytes(body)
    return str(target)


def _is_textual(content_type: str) -> bool:
    ctype = (content_type or "").lower()
    return not ctype or "text" in ctype or "json" in ctype or "xml" in ctype


def _attach_body(data: dict, resp: HttpResponse, save_as: str | None, ctx: ToolContext) -> None:
    body = resp.body
    textual = _is_textual(resp.content_type)
    if textual:
        text = body.decode("utf-8", errors="replace")
        if len(text) <= MAX_BODY_PREVIEW_CHARS:
            data["body"] = text
        else:
            data["body_preview"] = text[:MAX_BODY_PRETTY_CHARS]
    else:
        data["binary"] = True
    name = _safe_name(save_as or "", f"http-response-{resp.sha256[:12]}")
    try:
        path = _artifact_path(ctx, name, body)
        data["artifact"] = {"path": path, "bytes": len(body), "sha256": resp.sha256}
    except OSError as exc:
        data["artifact_error"] = f"Could not save response artifact: {exc.__class__.__name__}"


def http_request(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    method = str(input.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT, f"method must be one of: {sorted(ALLOWED_METHODS)}"
        )
    headers = _headers_input(input.get("headers"))
    if "headers" in input and headers is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "headers must be an object of string values")
    query = _query_input(input.get("query"))
    if "query" in input and query is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "query must be an object of string values")
    body = input.get("body")
    if body is not None and not isinstance(body, str):
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT, "body must be a string (use body_json for JSON)"
        )
    body_json = input.get("body_json")
    save_as = input.get("save_as")
    if save_as is not None and not isinstance(save_as, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "save_as must be a filename string")

    try:
        timeout_s = float(input.get("timeout_s") or 30.0)
        max_retries = int(input.get("max_retries") or 0)
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "timeout_s/max_retries must be numbers")
    follow = bool(input.get("follow_redirects", True))
    max_bytes = int(input.get("max_bytes") or MAX_RESPONSE_BYTES)
    max_bytes = max(4 * 1024, min(max_bytes, 16 * MAX_RESPONSE_BYTES))

    try:
        injection = apply_auth(input.get("auth"), ctx.credentials)
    except WebRequestError as exc:
        return _tool_err(exc)
    denied = _guard(ctx, url)
    if denied is not None:
        return denied

    token = ctx.cancellation
    if ctx.deadline_at is not None:
        timeout_s = min(timeout_s, ctx.deadline_at - time.time())
        if timeout_s <= 0:
            return _fail(ToolErrorCode.TIMEOUT, "HTTP deadline exhausted")
    is_cancelled = (lambda: bool(getattr(token, "cancelled", False))) if token is not None else None
    try:
        resp = request(
            url,
            method=method,
            headers={**(headers or {}), **injection.headers},
            query={**(query or {}), **injection.query} or None,
            content=body,
            body_json=body_json,
            follow_redirects=follow,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            max_retries=max_retries,
            client_factory=_client_factory(ctx),
            is_cancelled=is_cancelled,
        )
    except WebRequestError as exc:
        return _tool_err(exc)

    final_url = _redact_final_url(resp.final_url, injection)
    data: dict = {
        "status": resp.status,
        "success": resp.status < 400,
        "url": url,
        "final_url": final_url,
        "method": resp.method,
        "content_type": resp.content_type,
        "bytes": len(resp.body),
        "truncated": resp.truncated,
        "elapsed_ms": round(resp.elapsed_ms, 1),
        "attempts": resp.attempts,
        "headers": resp.headers,
    }
    if resp.retry_after_s is not None and resp.status == 429:
        data["retry_after_s"] = resp.retry_after_s
    if save_as or not _is_textual(resp.content_type) or len(resp.body) > MAX_BODY_PREVIEW_CHARS:
        _attach_body(data, resp, save_as, ctx)
    else:
        data["body"] = resp.body.decode("utf-8", errors="replace")
    return _ok(data)


def _redact_final_url(final_url: str, injection) -> str:
    if injection.secret_query_names:
        from rinari.http.auth import redact_url

        return redact_url(final_url, injection.secret_query_names)
    return final_url


def http_sse(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    headers = _headers_input(input.get("headers"))
    if "headers" in input and headers is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "headers must be an object of string values")
    query = _query_input(input.get("query"))
    if "query" in input and query is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "query must be an object of string values")
    event_filter = input.get("event_filter")
    if event_filter is not None and not isinstance(event_filter, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "event_filter must be a string")
    try:
        max_events = max(
            1, min(int(input.get("max_events") or MAX_SSE_EVENTS_DEFAULT), MAX_SSE_EVENTS_LIMIT)
        )
        timeout_s = float(input.get("timeout_s") or 30.0)
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "max_events/timeout_s must be numbers")
    timeout_s = max(1.0, min(timeout_s, 120.0))

    try:
        injection = apply_auth(input.get("auth"), ctx.credentials)
    except WebRequestError as exc:
        return _tool_err(exc)
    denied = _guard(ctx, url)
    if denied is not None:
        return denied

    started = time.monotonic()
    if ctx.deadline_at is not None:
        timeout_s = min(timeout_s, ctx.deadline_at - time.time())
        if timeout_s <= 0:
            return _fail(ToolErrorCode.TIMEOUT, "SSE deadline exhausted")
    if input.get("last_event_id"):
        headers = {**(headers or {}), "Last-Event-ID": str(input["last_event_id"])}
    deadline = started + timeout_s
    state = {"timed_out": False}

    def _lines_until_stop():
        for line in sse_lines(
            url,
            headers={**(headers or {}), **injection.headers},
            query={**(query or {}), **injection.query} or None,
            client_factory=_client_factory(ctx),
            timeout_s=timeout_s,
        ):
            if ctx.cancellation:
                ctx.cancellation.throw_if_cancelled()
            yield line
            if time.monotonic() >= deadline:
                state["timed_out"] = True
                return

    lines = _lines_until_stop()
    try:
        events, maxed = parse_sse_events(lines, max_events=max_events)
    except WebRequestError as exc:
        return _tool_err(exc)
    finally:
        lines.close()

    events = [
        {
            "event": ev.event,
            "data": ev.data[:MAX_SSE_DATA_CHARS],
            "id": ev.last_event_id,
            "retry_ms": ev.retry_ms,
        }
        for ev in events
        if event_filter is None or ev.event == event_filter
    ]
    return _ok(
        {
            "url": url,
            "events": events,
            "count": len(events),
            "stopped_early": maxed or state["timed_out"],
            "timed_out": state["timed_out"],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    )


def http_tools() -> list[ToolDefinition]:
    common = dict(capabilities=("network.outbound",), namespace="http")
    return [
        ToolDefinition(
            name="http.request",
            description=(
                "Generic HTTP request (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS) with "
                "headers, query, body, secret-ref auth, bounded retries and response artifact. "
                "Non-2xx statuses are reported as data, not as tool failures."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {
                        "type": "string",
                        "enum": sorted(ALLOWED_METHODS),
                    },
                    "headers": {"type": "object"},
                    "query": {"type": "object"},
                    "body": {"type": "string"},
                    "body_json": {"type": "object"},
                    "auth": {
                        "type": "object",
                        "description": (
                            "{type: bearer|basic|header|query, ref: secret-ref} "
                            "(+ username for basic, + name for header/query)"
                        ),
                    },
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                    "follow_redirects": {"type": "boolean"},
                    "max_bytes": {"type": "integer"},
                    "save_as": {
                        "type": "string",
                        "description": "Filename (artifact dir) to store the response body.",
                    },
                },
                "required": ["url"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            timeout_ms=150_000,
            handler=http_request,
            **common,
        ),
        ToolDefinition(
            name="http.sse",
            description=(
                "Consume a text/event-stream endpoint; returns bounded parsed "
                "SSE events (event, data, id, retry) within the timeout."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                    "query": {"type": "object"},
                    "auth": {"type": "object"},
                    "event_filter": {"type": "string"},
                    "last_event_id": {"type": "string", "maxLength": 1024},
                    "max_events": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SSE_EVENTS_LIMIT,
                    },
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
                },
                "required": ["url"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            timeout_ms=150_000,
            handler=http_sse,
            **common,
        ),
    ]


__all__ = ["http_tools"]
