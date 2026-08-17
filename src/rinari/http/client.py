"""Generic bounded HTTP transport for http.* tools (phase 5).

Extends the web transport (rinari.web.client, GET-only) with arbitrary
methods, request bodies, retries with backoff (idempotent methods only),
Retry-After handling, and an SSE line stream.

Design notes:

- A completed exchange with a 4xx/5xx status is a *data result* for
  http.request (the tool's job is to make the request and report the
  response); only transport-level failures (timeout, connect, DNS) raise
  WebRequestError and map to tool error codes.
- Header redaction happens here, at the transport boundary, so secrets can
  never reach the model or traces from a response.
- `client_factory` is the test seam (httpx.MockTransport); `sleep` and
  `is_cancelled` are seams for backoff tests and cancellation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from rinari.http.auth import redact_headers
from rinari.web.client import (
    MAX_RESPONSE_BYTES,
    WebRequestError,
    default_client_factory,
    map_status,
)

ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_DELAY_S = 30.0
BASE_BACKOFF_S = 0.5
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    final_url: str
    method: str
    status: int
    content_type: str
    headers: dict[str, str]
    body: bytes
    truncated: bool
    elapsed_ms: float
    attempts: int
    retry_after_s: int | None

    @property
    def sha256(self) -> str:
        import hashlib

        return hashlib.sha256(self.body).hexdigest()


def parse_retry_after(value: str | None, *, now: float | None = None) -> int | None:
    """Retry-After header (delta-seconds or HTTP-date) -> whole seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(int(value), int(MAX_RETRY_DELAY_S))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    reference = time.time() if now is None else now
    delay = int(when.timestamp() - reference)
    return max(0, min(delay, int(MAX_RETRY_DELAY_S)))


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    content: bytes | str | None = None,
    body_json: Any = None,
    follow_redirects: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_retries: int = 0,
    client_factory: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    is_cancelled: Callable[[], bool] | None = None,
) -> HttpResponse:
    """Perform one HTTP request; retry idempotent methods on 429/5xx + net errors."""
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise WebRequestError("INVALID_ARGUMENT", f"Unsupported HTTP method: {method}")
    if body_json is not None and content is not None:
        raise WebRequestError("INVALID_ARGUMENT", "Pass either body or body_json, not both")
    max_retries = max(0, min(int(max_retries), 5))
    timeout_s = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))

    payload: bytes | None = None
    request_headers: dict[str, str] = dict(headers or {})
    if body_json is not None:
        payload = json.dumps(body_json).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif content is not None:
        payload = content.encode("utf-8") if isinstance(content, str) else content
        if method not in ("GET", "HEAD"):
            request_headers.setdefault("Content-Type", "application/octet-stream")

    attempt = 0
    while True:
        attempt += 1
        _raise_if_cancelled(is_cancelled)
        client = (
            client_factory() if client_factory is not None else default_client_factory(timeout_s)
        )
        started = time.monotonic()
        transport_error: WebRequestError | None = None
        try:
            try:
                status, final_url, headers, ctype, body, truncated = _do_streamed(
                    client,
                    method,
                    url,
                    request_headers,
                    query,
                    payload,
                    follow_redirects,
                    max_bytes,
                )
            except httpx.StreamConsumed:
                # MockTransport seam: no raw stream available; full read.
                response = client.request(
                    method,
                    url,
                    headers=request_headers,
                    params=query or None,
                    content=payload,
                    follow_redirects=follow_redirects,
                )
                status = response.status_code
                final_url = str(response.url)
                ctype = response.headers.get("content-type", "")
                raw = response.content
                body = raw[:max_bytes]
                truncated = len(raw) > max_bytes
                headers = dict(response.headers)
            # httpx lowercases header names on access/iteration.
            retry_after = parse_retry_after(
                headers.get("retry-after") or headers.get("Retry-After")
            )
        except httpx.HTTPError as exc:
            transport_error = _map_transport_error(exc, timeout_s)
            retry_after = None
        finally:
            client.close()
        if transport_error is not None:
            if method in IDEMPOTENT_METHODS and attempt <= max_retries:
                _backoff_sleep(sleep, attempt)
                continue
            raise transport_error
        elapsed_ms = (time.monotonic() - started) * 1000

        if status in RETRYABLE_STATUSES and method in IDEMPOTENT_METHODS and attempt <= max_retries:
            delay = (
                retry_after if retry_after is not None else BASE_BACKOFF_S * (2 ** (attempt - 1))
            )
            sleep(float(min(delay, MAX_RETRY_DELAY_S)))
            continue
        return HttpResponse(
            url=url,
            final_url=final_url,
            method=method,
            status=status,
            content_type=ctype,
            headers=redact_headers(headers),
            body=bytes(body),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            attempts=attempt,
            retry_after_s=retry_after if status == 429 else None,
        )


def _do_streamed(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    query: dict[str, str] | None,
    payload: bytes | None,
    follow_redirects: bool,
    max_bytes: int,
) -> tuple[int, str, dict, str, bytes, bool]:
    with client.stream(
        method,
        url,
        headers=headers,
        params=query or None,
        content=payload,
        follow_redirects=follow_redirects,
    ) as response:
        body = bytearray()
        truncated = False
        for chunk in response.iter_raw():
            body.extend(chunk)
            if len(body) > max_bytes:
                truncated = True
                break
        return (
            response.status_code,
            str(response.url),
            dict(response.headers),
            response.headers.get("content-type", ""),
            bytes(body),
            truncated,
        )


def sse_lines(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    follow_redirects: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client_factory: Any = None,
) -> Iterator[str]:
    """Yield raw lines of a text/event-stream response (bounded by timeout)."""
    client = client_factory() if client_factory is not None else default_client_factory(timeout_s)
    try:
        try:
            with client.stream(
                "GET",
                url,
                headers=headers,
                params=query or None,
                follow_redirects=follow_redirects,
            ) as response:
                error = map_status(response.status_code)
                if error is not None:
                    raise error
                ctype = response.headers.get("content-type", "")
                if "text/event-stream" not in ctype.lower():
                    raise WebRequestError(
                        "INVALID_ARGUMENT",
                        f"Expected text/event-stream, got {ctype or 'no content-type'}",
                    )
                yield from response.iter_lines()
        except httpx.StreamConsumed:
            response = client.get(url, headers=headers, params=query or None)
            error = map_status(response.status_code)
            if error is not None:
                error.status = response.status_code
                raise error from None
            ctype = response.headers.get("content-type", "")
            if "text/event-stream" not in ctype.lower():
                raise WebRequestError(
                    "INVALID_ARGUMENT",
                    f"Expected text/event-stream, got {ctype or 'no content-type'}",
                ) from None
            yield from response.text.splitlines()
    finally:
        client.close()


def _raise_if_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise WebRequestError("CANCELLED", "Request cancelled")


def _map_transport_error(exc: Exception, timeout_s: float) -> WebRequestError:
    if isinstance(exc, httpx.TimeoutException):
        return WebRequestError(
            "TIMEOUT",
            f"Request timed out after {timeout_s:.0f}s: {exc.__class__.__name__}",
            retryable=True,
        )
    return WebRequestError(
        "NETWORK_ERROR", f"Connection failed: {exc.__class__.__name__}", retryable=True
    )


def _backoff_sleep(sleep: Callable[[float], None], attempt: int) -> None:
    delay = min(BASE_BACKOFF_S * (2 ** (attempt - 1)), MAX_RETRY_DELAY_S)
    sleep(float(delay))


__all__ = [
    "ALLOWED_METHODS",
    "BASE_BACKOFF_S",
    "DEFAULT_TIMEOUT_S",
    "IDEMPOTENT_METHODS",
    "MAX_TIMEOUT_S",
    "RETRYABLE_STATUSES",
    "HttpResponse",
    "parse_retry_after",
    "request",
    "sse_lines",
]
