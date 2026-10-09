"""MCP Streamable HTTP transport (MCP spec 2025-03-26, "Streamable HTTP").

One endpoint URL; every JSON-RPC message from the client is an HTTP POST:

- requests are sent with ``Accept: application/json, text/event-stream``; the
  server answers either with one JSON body or with an SSE stream that carries
  the response (and possibly server notifications/requests before it);
- notifications are answered with ``202 Accepted`` and no body;
- the ``Mcp-Session-Id`` header the server returns on ``initialize`` is sent
  back on every later request, and ``MCP-Protocol-Version`` once negotiated;
- closing the transport sends ``DELETE`` for the session when one exists.

TLS verification is always on. The legacy HTTP+SSE transport (2024-11-05:
``GET /sse`` + ``endpoint`` event) is **not** supported; a server that only
speaks it is reported as ``TRANSPORT_UNSUPPORTED`` (hint
``legacy_sse_unsupported``).

Secrets: header values (``Authorization`` etc.) are never part of any error
message, and URLs in messages are reduced to scheme://host[:port]/path so a
token in a query string never leaks either.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .protocol import McpMessage, parse_message
from .transport import McpTransport, TransportError

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
#: Version requested on `initialize` over HTTP (Streamable HTTP appeared in it).
HTTP_PROTOCOL_VERSION = "2025-03-26"
_ACCEPT = "application/json, text/event-stream"
_CONNECT_TIMEOUT_CAP_S = 10.0


def safe_url(url: str) -> str:
    """scheme://host[:port]/path — without userinfo, query or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        text = str(current)
        if "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text or "TLS" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


class StreamableHttpTransport(McpTransport):
    preferred_protocol_version = HTTP_PROTOCOL_VERSION

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        verify: bool | ssl.SSLContext = True,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._timeout_s = timeout_s
        self._verify = verify
        self._client = client
        self._owns_client = client is None
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._lock = threading.Lock()
        self._closed = False
        if client is not None:
            client.event_hooks = {
                **client.event_hooks,
                "request": [*client.event_hooks.get("request", []), self._same_origin_only],
            }

    def _same_origin_only(self, request: httpx.Request) -> None:
        # Redirects are followed only within the configured origin: httpx
        # strips `Authorization` on a cross-origin redirect but forwards every
        # other header, and a secret header (an API key under any name) must
        # never reach a host the owner did not configure.
        if _origin(request.url) != _origin(httpx.URL(self._url)):
            raise TransportError(
                "TRANSPORT_HTTP",
                f"{safe_url(self._url)} redirected to another origin "
                f"({safe_url(str(request.url))}); not followed so its credentials stay "
                "with the configured server. Use the final URL.",
                hint="check_url_or_network",
            )

    # -- introspection (tests/diagnostics; never secrets) ---------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.Client(
            verify=self._verify,
            follow_redirects=True,
            event_hooks={"request": [self._same_origin_only]},
            timeout=httpx.Timeout(
                self._timeout_s, connect=min(self._timeout_s, _CONNECT_TIMEOUT_CAP_S)
            ),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client = self._client
        if client is None:
            return
        if self._session_id is not None:
            # 405 or unreachable: the server ends the session on its own.
            with contextlib.suppress(httpx.HTTPError, TransportError):
                client.delete(self._url, headers=self._request_headers(), timeout=5.0)
        self._session_id = None
        if self._owns_client:
            client.close()
        self._client = None

    # -- wire -----------------------------------------------------------------

    def _request_headers(self) -> dict[str, str]:
        headers = {
            **self._headers,
            "Accept": _ACCEPT,
            "Content-Type": "application/json",
        }
        if self._session_id is not None:
            headers[SESSION_HEADER] = self._session_id
        if self._protocol_version is not None:
            headers[PROTOCOL_HEADER] = self._protocol_version
        return headers

    def send(self, payload: str, timeout_s: float) -> McpMessage:
        if self._closed:
            raise TransportError("TRANSPORT_CLOSED", "transport is closed")
        if self._client is None:
            self.start()
        try:
            sent = json.loads(payload)
        except ValueError as exc:
            raise TransportError("TRANSPORT_IO", "invalid outgoing frame") from exc
        expected = sent.get("id") if isinstance(sent, dict) else None
        method = sent.get("method") if isinstance(sent, dict) else None
        with self._lock:
            message = self._post_request(payload, expected, timeout_s)
        if method == "initialize" and isinstance(message.result, dict):
            version = message.result.get("protocolVersion")
            if isinstance(version, str) and version:
                self._protocol_version = version
        return message

    def notify(self, payload: str) -> None:
        if self._closed:
            return
        if self._client is None:
            self.start()
        assert self._client is not None
        with self._lock:
            try:
                response = self._client.post(
                    self._url,
                    content=payload.encode("utf-8"),
                    headers=self._request_headers(),
                    timeout=min(self._timeout_s, 10.0),
                )
                response.close()
            except (httpx.HTTPError, TransportError):
                pass  # Notifications are fire-and-forget.

    def _post_request(self, payload: str, expected: Any, timeout_s: float) -> McpMessage:
        assert self._client is not None
        deadline = time.monotonic() + timeout_s
        timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, _CONNECT_TIMEOUT_CAP_S))
        try:
            with self._client.stream(
                "POST",
                self._url,
                content=payload.encode("utf-8"),
                headers=self._request_headers(),
                timeout=timeout,
            ) as response:
                session = response.headers.get(SESSION_HEADER)
                if session:
                    self._session_id = session
                self._raise_for_status(response)
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                content_type = content_type.lower()
                if content_type == "text/event-stream":
                    return self._from_sse(response, expected, deadline)
                if content_type == "application/json":
                    response.read()
                    return self._from_json(response.content, expected)
                if response.status_code == 202:
                    raise TransportError(
                        "TRANSPORT_PROTOCOL",
                        "server accepted the request without answering it",
                        http_status=202,
                        hint="not_an_mcp_endpoint",
                    )
                raise TransportError(
                    "TRANSPORT_PROTOCOL",
                    f"unexpected response content type {content_type or 'none'!r} "
                    f"from {safe_url(self._url)}",
                    http_status=response.status_code,
                    hint="not_an_mcp_endpoint",
                )
        except TransportError:
            raise
        except httpx.ConnectTimeout as exc:
            # No answer to the connection itself: the host is not reachable
            # (firewall, wrong host/port), not a slow MCP server.
            raise TransportError(
                "TRANSPORT_UNREACHABLE",
                f"connecting to {safe_url(self._url)} timed out",
                hint="check_url_or_network",
            ) from exc
        except httpx.TimeoutException as exc:
            raise TransportError(
                "TRANSPORT_TIMEOUT",
                f"server did not answer within {timeout_s:g}s",
                hint="server_slow_or_unresponsive",
            ) from exc
        except httpx.ConnectError as exc:
            if _is_tls_error(exc):
                raise TransportError(
                    "TRANSPORT_TLS",
                    f"TLS handshake with {safe_url(self._url)} failed (certificate not trusted "
                    "or TLS error)",
                    hint="check_tls_certificate",
                ) from exc
            raise TransportError(
                "TRANSPORT_UNREACHABLE",
                f"cannot connect to {safe_url(self._url)}",
                hint="check_url_or_network",
            ) from exc
        except httpx.HTTPError as exc:
            if _is_tls_error(exc):
                raise TransportError(
                    "TRANSPORT_TLS",
                    f"TLS error talking to {safe_url(self._url)}",
                    hint="check_tls_certificate",
                ) from exc
            raise TransportError(
                "TRANSPORT_IO",
                f"connection to {safe_url(self._url)} failed: {type(exc).__name__}",
                hint="check_url_or_network",
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in (401, 403):
            challenge = response.headers.get("www-authenticate", "")
            sent_auth = any(k.lower() == "authorization" for k in self._headers)
            hint = (
                "oauth_required"
                if "resource_metadata" in challenge and not sent_auth
                else "check_credentials"
            )
            raise TransportError(
                "TRANSPORT_AUTH",
                f"server rejected the credentials (HTTP {status})",
                http_status=status,
                hint=hint,
            )
        if status == 404:
            if self._session_id is not None:
                # Spec: the session ended; the client must initialize again.
                self._session_id = None
                self._protocol_version = None
                raise TransportError(
                    "TRANSPORT_SESSION_EXPIRED",
                    "the server ended the MCP session; reconnect",
                    http_status=404,
                    hint="session_expired",
                )
            raise TransportError(
                "TRANSPORT_NOT_FOUND",
                f"no MCP endpoint at {safe_url(self._url)} (HTTP 404)",
                http_status=404,
                hint="check_endpoint_path",
            )
        if status == 405:
            raise TransportError(
                "TRANSPORT_UNSUPPORTED",
                "the server does not accept POST at this URL; it may only speak the legacy "
                "HTTP+SSE transport, which Rinari does not support",
                http_status=405,
                hint="legacy_sse_unsupported",
            )
        raise TransportError(
            "TRANSPORT_HTTP",
            f"server answered HTTP {status}",
            http_status=status,
            hint="server_error" if status >= 500 else "request_rejected",
        )

    # -- response bodies ---------------------------------------------------------

    def _from_json(self, body: bytes, expected: Any) -> McpMessage:
        try:
            obj = json.loads(body.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(
                "TRANSPORT_PROTOCOL",
                "server response is not valid JSON",
                hint="not_an_mcp_endpoint",
            ) from exc
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if not isinstance(item, dict):
                continue
            message = McpMessage(raw=item)
            if message.is_response and (expected is None or message.id == expected):
                return message
        raise TransportError(
            "TRANSPORT_PROTOCOL",
            "server response does not answer the request",
            hint="not_an_mcp_endpoint",
        )

    def _from_sse(self, response: httpx.Response, expected: Any, deadline: float) -> McpMessage:
        for data in _sse_data(response.iter_lines()):
            if time.monotonic() > deadline:
                raise TransportError(
                    "TRANSPORT_TIMEOUT",
                    "server did not finish the response in time",
                    hint="server_slow_or_unresponsive",
                )
            message = parse_message(data)
            if message is None:
                continue
            if message.is_response and (expected is None or message.id == expected):
                return message
            if message.method and message.id is not None and not message.is_response:
                self._answer_server_request(message)
            # Server notifications (progress, logging…) are ignored.
        raise TransportError(
            "TRANSPORT_IO",
            "the server closed the event stream before answering",
            hint="server_error",
        )

    def _answer_server_request(self, message: McpMessage) -> None:
        """Reply to a server→client request seen on the stream.

        `ping` gets an empty result; anything else (sampling, roots…) is a
        capability this client never advertised: method not found.
        """
        if message.method == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message.id, "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message.id,
                "error": {"code": -32601, "message": "method not supported by client"},
            }
        assert self._client is not None
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                self._url,
                content=json.dumps(reply).encode("utf-8"),
                headers=self._request_headers(),
                timeout=min(self._timeout_s, 10.0),
            ).close()


def _sse_data(lines: Iterator[str]) -> Iterator[str]:
    """Yield the `data` payload of each SSE `message` event (spec framing)."""
    event = ""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data and event in ("", "message"):
                yield "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data and event in ("", "message"):
        yield "\n".join(data)


__all__ = [
    "HTTP_PROTOCOL_VERSION",
    "PROTOCOL_HEADER",
    "SESSION_HEADER",
    "StreamableHttpTransport",
    "safe_url",
]
