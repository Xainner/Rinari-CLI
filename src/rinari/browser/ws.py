"""Minimal RFC 6455 WebSocket client for the CDP driver (phase 5).

CDP only needs text frames over `ws://`, so a raw-socket client is the
smallest auditable way to get it without new dependencies (phase 5
"Browser engine" decision: direct CDP; documented alternative: Playwright).

Threading contract: reads happen only in the CDP session's reader thread;
writes (commands + pong replies) are serialized under a lock.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import select
import socket
import struct
import threading
import time
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WsError(Exception):
    """Structured WebSocket failure (maps to browser tool error codes)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code  # WS_CONNECT | WS_TIMEOUT | WS_PROTOCOL | WS_CLOSED
        self.message = message
        self.retryable = retryable


class WebSocketClient:
    def __init__(self, timeout_s: float = 10.0) -> None:
        self._timeout_s = timeout_s
        self._max_message_bytes = MAX_MESSAGE_BYTES
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._incoming = bytearray()
        self._message = bytearray()
        self._message_opcode: int | None = None
        self._read_deadline: float | None = None

    @property
    def closed(self) -> bool:
        return self._closed or self._sock is None

    # -- connection --------------------------------------------------------

    def connect(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise WsError("WS_PROTOCOL", f"Unsupported WebSocket URL: {url!r}")
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self._incoming.clear()
        self._message.clear()
        self._message_opcode = None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            sock = socket.create_connection((host, port), timeout=self._timeout_s)
        except (TimeoutError, OSError) as exc:
            raise WsError(
                "WS_CONNECT", f"Could not reach {host}:{port}: {exc}", retryable=True
            ) from exc
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.settimeout(self._timeout_s)
        try:
            sock.sendall(request.encode("ascii"))
            status, headers = self._read_http_response(sock)
        except WsError:
            sock.close()
            raise
        except (TimeoutError, OSError) as exc:
            sock.close()
            raise WsError("WS_CONNECT", f"Handshake failed: {exc}", retryable=True) from exc
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode(
            "ascii"
        )
        if status != 101 or headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise WsError("WS_PROTOCOL", f"Handshake rejected (HTTP {status}, bad accept key)")
        self._sock = sock
        self._closed = False

    def close(self) -> None:
        sock = None
        with self._lock:
            sock, self._sock = self._sock, None
            self._closed = True
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.settimeout(2.0)
            sock.sendall(self._build_frame(OP_CLOSE, struct.pack(">H", 1000)))
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()

    # -- I/O ----------------------------------------------------------------

    def send(self, text: str) -> None:
        frame = self._build_frame(OP_TEXT, text.encode("utf-8"))
        with self._lock:
            sock = self._sock
            if sock is None:
                raise WsError("WS_CLOSED", "Socket is closed")
            try:
                sock.sendall(frame)
            except (TimeoutError, OSError) as exc:
                raise WsError("WS_CLOSED", f"Send failed: {exc}", retryable=True) from exc

    def recv(self, timeout_s: float | None = None) -> str | None:
        """Read one complete text message; None when the peer closed."""
        sock = self._sock
        if sock is None:
            raise WsError("WS_CLOSED", "Socket is closed")
        self._read_deadline = time.monotonic() + (timeout_s or self._timeout_s)
        data = self._message
        message_opcode = self._message_opcode
        try:
            while True:
                opcode, fin, payload = self._read_frame(sock)
                if opcode == OP_PING:
                    with self._lock, contextlib.suppress(TimeoutError, OSError):
                        sock.sendall(self._build_frame(OP_PONG, payload))
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    self.close()
                    return None
                if message_opcode is None:
                    if opcode == OP_CONTINUATION:
                        raise WsError("WS_PROTOCOL", "Unsolicited continuation frame")
                    message_opcode = opcode
                    self._message_opcode = opcode
                elif opcode != OP_CONTINUATION:
                    raise WsError("WS_PROTOCOL", "New message before continuation finished")
                data.extend(payload)
                if len(data) > self._max_message_bytes:
                    raise WsError("WS_PROTOCOL", "Message exceeds size budget")
                if fin:
                    break
        except TimeoutError as exc:
            # Inactivity is not disconnect. Keep partial frames AND message fragments.
            raise WsError("WS_TIMEOUT", "Timed out waiting for a frame", retryable=True) from exc
        except WsError:
            self._teardown()
            raise
        except OSError as exc:
            self._teardown()
            raise WsError("WS_CLOSED", f"Connection lost: {exc}", retryable=True) from exc
        if message_opcode not in (OP_TEXT, OP_BINARY):
            raise WsError("WS_PROTOCOL", f"Unsupported message opcode: {message_opcode:#x}")
        result = data.decode("utf-8", errors="replace")
        self._message.clear()
        self._message_opcode = None
        return result

    def _teardown(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._closed = True
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    # -- frames ---------------------------------------------------------------

    def _build_frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(header) + masked

    def _read_frame(self, sock: socket.socket) -> tuple[int, bool, bytes]:
        self._fill(sock, 2)
        b0, b1 = self._incoming[:2]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        if b1 & 0x80:
            raise WsError("WS_PROTOCOL", "Server frame is masked (RFC 6455 violation)")
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            self._fill(sock, 4)
            (length,) = struct.unpack(">H", self._incoming[2:4])
            offset = 4
        elif length == 127:
            self._fill(sock, 10)
            (length,) = struct.unpack(">Q", self._incoming[2:10])
            offset = 10
        if length > self._max_message_bytes:
            raise WsError("WS_PROTOCOL", f"Frame too large: {length} bytes")
        self._fill(sock, offset + length)
        payload = bytes(self._incoming[offset:offset + length])
        del self._incoming[:offset + length]
        return opcode, fin, payload

    def _fill(self, sock: socket.socket, n: int) -> None:
        while len(self._incoming) < n:
            remaining = max(0, (self._read_deadline or time.monotonic()) - time.monotonic())
            if not select.select([sock], [], [], remaining)[0]:
                raise TimeoutError("No frame data before read deadline")
            chunk = sock.recv(n - len(self._incoming))
            if not chunk:
                raise WsError("WS_CLOSED", "Connection closed by peer")
            self._incoming.extend(chunk)

    def _read_http_response(self, sock: socket.socket) -> tuple[int, dict[str, str]]:
        buf = bytearray()
        while b"\r\n\r\n" not in bytes(buf):
            chunk = sock.recv(4096)
            if not chunk:
                raise WsError("WS_PROTOCOL", "Connection closed during handshake")
            buf.extend(chunk)
            if len(buf) > 65536:
                raise WsError("WS_PROTOCOL", "Handshake response too large")
        header, remainder = buf.split(b"\r\n\r\n", 1)
        self._incoming.extend(remainder)
        head = bytes(header).decode("iso-8859-1")
        lines = head.split("\r\n")
        parts = lines[0].split(" ", 2)
        status = int(parts[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        return status, headers


__all__ = ["MAX_MESSAGE_BYTES", "WebSocketClient", "WsError"]
