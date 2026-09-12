"""LSP client core: JSON-RPC over stdio with LSP framing (phase 3 LSP).

Speaks the Language Server Protocol base layer: Content-Length framed
JSON-RPC, initialize/initialized handshake, textDocument sync (didOpen /
didChange / didClose, full sync), request/response correlation,
notifications, shutdown/exit. Server-side capabilities are stored for the
manager's capability gating.

Everything is subprocess-local: no network, no shared state.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CRASHED = object()

DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_REQUEST_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 5.0


class LspError(Exception):
    """Base class for all LSP client errors."""


class LspProtocolError(LspError):
    """Framing or JSON-RPC structure problem."""


class LspServerError(LspError):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LspServerCrashed(LspError):
    """The server process exited or its stdio closed."""


class LspRequestTimeout(LspError):
    """The server did not answer within the deadline."""


class LspNotStarted(LspError):
    """The client was asked to operate before start()."""


@dataclass(frozen=True, slots=True)
class LspServerSpec:
    name: str
    languages: tuple[str, ...]
    command: tuple[str, ...]
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT


def encode_message(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_message(stream) -> dict | None:
    """Read one framed message. Returns None on EOF."""
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if headers:
                break
            continue
        if b":" not in line:
            raise LspProtocolError(f"invalid LSP header line: {line!r}")
        key, _, value = line.decode("ascii").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = headers.get("content-length")
    if not length or not length.isdigit():
        raise LspProtocolError(f"missing Content-Length header: {headers!r}")
    body = stream.read(int(length))
    if body is None or len(body) < int(length):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LspProtocolError(f"invalid JSON in LSP message: {exc}") from exc


class _Reader(threading.Thread):
    def __init__(self, client: LspClient, stream) -> None:
        super().__init__(name="lsp-reader", daemon=True)
        self._client = client
        self._stream = stream

    def run(self) -> None:
        while True:
            try:
                message = read_message(self._stream)
            except LspError:
                break
            if message is None:
                break
            self._client._on_message(message)
        self._client._on_crash()


class LspClient:
    def __init__(self, spec: LspServerSpec, root: Path) -> None:
        self.spec = spec
        self.root = Path(root)
        self.capabilities: dict = {}
        self.server_info: dict = {}
        self.started = False
        self.closed = False
        self._proc: subprocess.Popen | None = None
        self._reader: _Reader | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._diagnostics: dict[str, list[dict]] = {}
        self._diagnostic_versions: dict[str, int | None] = {}
        self._doc_versions: dict[str, int] = {}
        self._alive = threading.Event()
        self._inbox: queue.Queue = queue.Queue(maxsize=64)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self.started:
            return
        self._proc = subprocess.Popen(
            list(self.spec.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.root),
        )
        assert self._proc.stdout is not None
        self._reader = _Reader(self, self._proc.stdout)
        self._reader.start()
        self._alive.set()
        self.started = True  # RPC is usable; the server is unverified until initialize answers
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root.as_uri(),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "capabilities": {
                    "workspace": {"workspaceFolders": True},
                    "textDocument": {
                        "publishDiagnostics": {},
                        "synchronization": {"didSave": True},
                    },
                },
            },
            timeout=self.spec.startup_timeout,
        )
        if not isinstance(result, dict):
            self.close()
            raise LspProtocolError("initialize did not return an object")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.notify("initialized", {})

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._alive.clear()
        self._fail_pending()
        if self._proc is None:
            return
        try:
            self.request("shutdown", {}, timeout=SHUTDOWN_TIMEOUT)
            self.notify("exit", {})
        except LspError:
            pass
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            pass
        if self._reader is not None:
            self._reader.join(timeout=SHUTDOWN_TIMEOUT)

    def _fail_pending(self) -> None:
        pending = list(self._pending.values())
        for item in pending:
            item.put(_CRASHED)

    # -- wire -----------------------------------------------------------

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspServerCrashed("server process is not running")
        try:
            self._proc.stdin.write(encode_message(payload))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self._alive.clear()
            self._fail_pending()
            raise LspServerCrashed("cannot write to LSP server stdio") from None

    def _on_message(self, message: dict) -> None:
        if "id" in message and "method" not in message:
            item = self._pending.pop(int(message["id"]), None)
            if item is not None:
                item.put(message)
            return
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = params.get("uri")
            if isinstance(uri, str):
                with self._lock:
                    version = params.get("version")
                    current = self._doc_versions.get(uri)
                    if current is None or (version is not None and version != current):
                        return
                    self._diagnostics[uri] = list(params.get("diagnostics") or [])
                    self._diagnostic_versions[uri] = version
        with contextlib.suppress(queue.Full):
            self._inbox.put_nowait(message)

    def _on_crash(self) -> None:
        self._alive.clear()
        self._fail_pending()

    @property
    def alive(self) -> bool:
        return self._alive.is_set() and not self.closed

    # -- RPC ------------------------------------------------------------

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        if not self.started:
            raise LspNotStarted(f"{self.spec.name} is not started")
        if not self.alive:
            raise LspServerCrashed(f"server {self.spec.name} is not running")
        deadline = timeout if timeout is not None else self.spec.request_timeout
        import time

        from rinari.shared.execution_scope import execution_context

        context = execution_context.get()
        if context and context.deadline_at is not None:
            deadline = min(deadline, max(0, context.deadline_at - time.time()))
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        waiter = queue.Queue(maxsize=1)
        self._pending[request_id] = waiter
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            try:
                end = time.monotonic() + deadline
                while True:
                    if context and context.cancellation:
                        if context.cancellation.cancelled:
                            self.notify("$/cancelRequest", {"id": request_id})
                        context.cancellation.throw_if_cancelled()
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    try:
                        item = waiter.get(timeout=min(remaining, 0.1))
                        break
                    except queue.Empty:
                        continue
            except queue.Empty:
                raise LspRequestTimeout(f"no answer to {method} within {deadline}s") from None
        finally:
            self._pending.pop(request_id, None)
        if item is _CRASHED:
            raise LspServerCrashed(f"server {self.spec.name} exited mid-request")
        if "error" in item:
            raw = item.get("error") or {}
            raise LspServerError(
                int(raw.get("code", -1)), str(raw.get("message", "unknown LSP error"))
            )
        return item.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        if not self.started:
            raise LspNotStarted(f"{self.spec.name} is not started")
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- documents --------------------------------------------------------

    @staticmethod
    def uri_for(path: Path) -> str:
        return Path(path).as_uri()

    def open_document(self, path: Path, text: str, language_id: str) -> None:
        uri = self.uri_for(path)
        with self._lock:
            self._doc_versions[uri] = self._doc_versions.get(uri, 0) + 1
            version = self._doc_versions[uri]
            self._diagnostics.pop(uri, None)
            self._diagnostic_versions.pop(uri, None)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    def change_document(self, path: Path, text: str) -> None:
        uri = self.uri_for(path)
        with self._lock:
            self._doc_versions[uri] = self._doc_versions.get(uri, 0) + 1
            version = self._doc_versions[uri]
            self._diagnostics.pop(uri, None)
            self._diagnostic_versions.pop(uri, None)
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )

    def close_document(self, path: Path) -> None:
        uri = self.uri_for(path)
        with self._lock:
            if uri in self._doc_versions:
                self._doc_versions.pop(uri, None)
                self._diagnostics.pop(uri, None)
                self._diagnostic_versions.pop(uri, None)
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def diagnostics(self, path: Path) -> list[dict]:
        return list(self._diagnostics.get(self.uri_for(path), []))

    def diagnostics_state(self, path: Path) -> dict:
        uri = self.uri_for(path)
        with self._lock:
            reported = self._diagnostic_versions.get(uri)
            current = self._doc_versions.get(uri)
            return {
                "status": "received" if uri in self._diagnostics else "waiting",
                "document_version": current,
                "reported_version": reported,
                "version_verified": reported is not None and reported == current,
            }

    def is_document_open(self, uri: str) -> bool:
        return uri in self._doc_versions
