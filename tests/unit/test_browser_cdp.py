"""Browser (CDP) tests — phase 5 "Browser engine" (direct CDP decision).

Deterministic and network-isolated: everything runs against a loopback
fake CDP *server* (HTTP /json/version + a WebSocket endpoint speaking a
scripted subset of CDP). No real browser is required; the RFC6455 client
is exercised frame by frame.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from rinari.browser.cdp import CdpError, CdpSession
from rinari.browser.manager import BrowserError, BrowserManager
from rinari.browser.ws import WebSocketClient, WsError
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.browse import browse_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_PNG = base64.b64encode(b"\x89PNG fake bytes").decode("ascii")


class _CdpRouteError(Exception):
    pass


# -- fake CDP server -----------------------------------------------------------------


class FakeCdpServer:
    """Loopback CDP endpoint: HTTP /json/version + scripted WS/CDP router."""

    def __init__(self, *, big_html: bool = False) -> None:
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.settimeout(5.0)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(16)
        self.port = self._listen.getsockname()[1]
        # endpoint contract: the CDP server root (like ws://127.0.0.1:9222);
        # /json/version lives at the root and points at the browser WS below.
        self.base_url = f"ws://127.0.0.1:{self.port}"
        self.cdp_url = f"{self.base_url}/devtools/browser/fake"
        self._stop = threading.Event()
        self.big_html = big_html
        self.targets: dict[str, dict] = {
            "t1": {"type": "page", "title": "Home", "url": "about:blank"},
            "t2": {"type": "page", "title": "Docs", "url": "https://example.com/"},
            "sw1": {
                "type": "service_worker",
                "title": "",
                "url": "https://example.com/sw.js",
            },
        }
        self._next_target = 3
        self._sessions: dict[str, str] = {}
        self._session_target: dict[str, str] = {}
        self._next_session = 1
        self.commands: list[tuple[str, dict, str | None]] = []
        self.download_file: bytes | None = None
        self.last_pong: bytes | None = None
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> FakeCdpServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for conn in list(self._conns):
            with suppress(OSError):
                conn.close()
        with suppress(OSError):
            self._listen.close()

    def drop(self) -> None:
        for conn in list(self._conns):
            with suppress(OSError):
                conn.close()

    # -- sockets ------------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listen.accept()
            except (TimeoutError, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        self._conns.append(conn)
        try:
            request = self._read_http_request(conn)
            if request is None:
                return
            path, headers = request
            if "websocket" in headers.get("upgrade", "").lower():
                self._ws_handshake(conn, headers.get("sec-websocket-key", ""))
                self._ws_loop(conn)
            elif path == "/json/version":
                self._send_json(
                    conn, {"Browser": "FakeChromium/1.0", "webSocketDebuggerUrl": self.cdp_url}
                )
            else:
                self._send_json(conn, {"error": "not found"}, status=404)
        except (TimeoutError, OSError):
            pass
        finally:
            with suppress(OSError):
                conn.close()
            if conn in self._conns:
                self._conns.remove(conn)

    @staticmethod
    def _read_exact(conn: socket.socket, n: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < n:
            chunk = conn.recv(n - len(buffer))
            if not chunk:
                raise OSError("closed")
            buffer.extend(chunk)
        return bytes(buffer)

    def _read_http_request(self, conn: socket.socket):
        buffer = bytearray()
        while b"\r\n\r\n" not in bytes(buffer):
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buffer.extend(chunk)
            if len(buffer) > 65536:
                return None
        head = bytes(buffer.split(b"\r\n\r\n", 1)[0]).decode("iso-8859-1")
        lines = head.split("\r\n")
        _method, path, _protocol = lines[0].split(" ", 2)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        return path, headers

    def _send_json(self, conn: socket.socket, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        conn.sendall(
            (
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'Not Found'}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + body
        )

    def _ws_handshake(self, conn: socket.socket, request_key: str) -> None:
        accept = base64.b64encode(
            hashlib.sha1((request_key + _GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )

    def _ws_send_frame(
        self, conn: socket.socket, opcode: int, payload: bytes, fin: bool = True
    ) -> None:
        header = bytearray([0x80 | opcode if fin else opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        conn.sendall(bytes(header) + payload)

    def _ws_send(self, conn: socket.socket, text: str) -> None:
        self._ws_send_frame(conn, 0x1, text.encode("utf-8"))

    def _read_ws_frame(self, conn: socket.socket) -> tuple[int, bool, bytes]:
        b0, b1 = self._read_exact(conn, 2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(conn, 2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(conn, 8))
        mask = self._read_exact(conn, 4) if masked else b""
        payload = self._read_exact(conn, length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, fin, payload

    def _ws_loop(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                opcode, fin, payload = self._read_ws_frame(conn)
            except (TimeoutError, OSError):
                return
            if opcode == 0x8:
                with suppress(OSError):
                    self._ws_send_frame(conn, 0x8, struct.pack(">H", 1000))
                return
            if opcode == 0x9:
                with suppress(OSError):
                    self._ws_send_frame(conn, 0xA, payload)
                continue
            if not fin:
                continue  # clients here send single text frames
            text = payload.decode("utf-8", "replace")
            if text.startswith("SLOW:"):
                time.sleep(1.0)
                with suppress(OSError):
                    self._ws_send(conn, text)
                continue
            if text == "PING-ME":
                with suppress(OSError):
                    self._ws_send_frame(conn, 0x9, b"\x01\x02")
                try:
                    pong_op, _, pong_payload = self._read_ws_frame(conn)
                    self.last_pong = pong_payload if pong_op == 0xA else None
                except (TimeoutError, OSError):
                    self.last_pong = None
                with suppress(OSError):
                    self._ws_send(conn, "PING-ME")
                continue
            if text == "FRAG":
                with suppress(OSError):
                    self._ws_send_frame(conn, 0x1, b"Hel", fin=False)
                    self._ws_send_frame(conn, 0x0, b"lo CDP")
                continue
            if text == "CLOSE":
                with suppress(OSError):
                    self._ws_send_frame(conn, 0x8, struct.pack(">H", 1000))
                continue
            try:
                message = json.loads(text)
            except ValueError:
                with suppress(OSError):
                    self._ws_send(conn, text)
                continue
            self._dispatch(conn, message)

    def _dispatch(self, conn: socket.socket, message: dict) -> None:
        message_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {}) or {}
        session_id = message.get("sessionId")
        if session_id:
            self._session_target.setdefault(
                session_id, params.get("targetId") or self._session_target.get(session_id)
            )
        self.commands.append((method, params, session_id))
        close_after = False
        events: list[dict] = []
        try:
            result, close_after, events = self._route(method, params, session_id)
        except _CdpRouteError as exc:
            with suppress(OSError):
                self._ws_send(
                    conn,
                    json.dumps({"id": message_id, "error": {"code": -32601, "message": str(exc)}}),
                )
            return
        with suppress(OSError):
            self._ws_send(
                conn, json.dumps({"id": message_id, "result": result if result is not None else {}})
            )
            for event in events:
                self._ws_send(conn, json.dumps(event))
        if close_after:
            with suppress(OSError):
                self._ws_send_frame(conn, 0x8, struct.pack(">H", 1000))

    # -- scripted CDP ---------------------------------------------------------

    def _route(self, method: str, params: dict, session_id: str | None):
        if method == "Target.getTargets":
            infos = [{"targetId": tid, **info} for tid, info in self.targets.items()]
            return {"targetInfos": infos}, False, []
        if method == "Target.createTarget":
            tid = f"t{self._next_target}"
            self._next_target += 1
            self.targets[tid] = {
                "type": "page",
                "title": "New",
                "url": params.get("url") or "about:blank",
            }
            return {"targetId": tid}, False, []
        if method == "Target.attachToTarget":
            tid = params.get("targetId")
            if tid not in self.targets:
                raise _CdpRouteError(f"No such target id: {tid}")
            if tid not in self._sessions:
                sid = f"s{self._next_session}"
                self._next_session += 1
                self._sessions[tid] = sid
            self._session_target[self._sessions[tid]] = tid
            return {"sessionId": self._sessions[tid]}, False, []
        if method == "Target.closeTarget":
            target_id = params.get("targetId")
            self.targets.pop(target_id, None)
            closed_session = self._sessions.pop(target_id, None)
            if closed_session is not None:
                self._session_target.pop(closed_session, None)
            return {}, False, []
        if method == "Target.slow":
            time.sleep(1.5)
            return {}, False, []
        if method == "Browser.close":
            return {}, True, []
        if method == "Browser.setDownloadBehavior":
            path = params.get("downloadPath")
            if path and self.download_file is not None:
                blob = self.download_file

                def write_download() -> None:
                    time.sleep(0.3)
                    with suppress(OSError), open(Path(path) / "report.pdf", "wb") as handle:
                        handle.write(blob)

                threading.Thread(target=write_download, daemon=True).start()
            return {}, False, []
        if method == "Runtime.enable":
            if session_id:
                events = [
                    {
                        "method": "Runtime.consoleAPICalled",
                        "params": {
                            "type": "log",
                            "args": [
                                {"type": "string", "value": "hello from page"},
                                {"type": "number", "value": 42},
                            ],
                        },
                        "sessionId": session_id,
                    }
                ]
                return {}, False, events
            return {}, False, []
        if method == "Network.enable":
            if session_id:
                events = [
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "requestId": "r1",
                            "type": "Document",
                            "request": {"url": "https://example.com/", "method": "GET"},
                        },
                        "sessionId": session_id,
                    },
                    {
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "r1",
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "mimeType": "text/html",
                            },
                        },
                        "sessionId": session_id,
                    },
                ]
                return {}, False, events
            return {}, False, []
        if method in ("Page.enable", "Accessibility.enable", "DOM.enable"):
            return {}, False, []
        if method == "Runtime.evaluate":
            inner = self._evaluate(params.get("expression", ""))
            if "exceptionDetails" in inner:
                details = inner["exceptionDetails"]
                return (
                    {
                        "result": {k: v for k, v in inner.items() if k != "exceptionDetails"},
                        "exceptionDetails": details,
                    },
                    False,
                    [],
                )
            return {"result": inner}, False, []
        if method == "Page.navigate":
            url = params.get("url", "")
            target_id = self._session_target.get(session_id or "", "")
            if target_id in self.targets:
                self.targets[target_id]["url"] = url
            if "blocked.example" in url:
                return (
                    {
                        "frameId": "frame-0",
                        "loaderId": "L1",
                        "errorText": "net::ERR_BLOCKED_BY_FAKE",
                    },
                    False,
                    [],
                )
            return {"frameId": "frame-0", "loaderId": "L1"}, False, []
        if method == "Page.captureScreenshot":
            return {"data": _PNG}, False, []
        if method == "Accessibility.getFullAXTree":
            return (
                {
                    "nodes": [
                        {
                            "nodeId": "n1",
                            "role": {"value": "RootWebArea"},
                            "name": {"value": "Home"},
                            "value": "",
                            "childIds": ["n2", "n3"],
                        },
                        {
                            "nodeId": "n2",
                            "role": {"value": "link"},
                            "name": {"value": "Go"},
                            "value": "",
                            "parentId": "n1",
                            "childIds": [],
                        },
                        {
                            "nodeId": "n3",
                            "role": {"value": "button"},
                            "name": {"value": "Press"},
                            "value": "",
                            "parentId": "n1",
                            "childIds": [],
                        },
                    ]
                },
                False,
                [],
            )
        if method == "Network.getCookies":
            return (
                {
                    "cookies": [
                        {
                            "name": "session",
                            "value": "super-secret-cookie-value",
                            "domain": "example.com",
                            "path": "/",
                            "httpOnly": True,
                            "secure": True,
                        }
                    ]
                },
                False,
                [],
            )
        if method in (
            "Network.setCookie",
            "Input.dispatchMouseEvent",
            "Input.dispatchKeyEvent",
            "DOM.setFileInputFiles",
        ):
            return {}, False, []
        raise _CdpRouteError(f"Unknown method: {method}")

    def _evaluate(self, expression: str) -> dict:
        if "document.documentElement" in expression:
            if self.big_html:
                html = "<html><body>" + ("x" * 300_000) + "</body></html>"
            else:
                html = "<html><body>fake page</body></html>"
            return {"type": "string", "value": html}
        if "window.innerWidth" in expression:
            return {"type": "object", "value": [1024, 768]}
        if "scrollIntoView" in expression:
            return {"type": "object", "value": {"x": 100.5, "y": 200.25}}
        if "__boom__" in expression:
            return {
                "type": "object",
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "Uncaught Error: boom"},
                },
            }
        if expression.startswith("document.querySelector("):
            return {"type": "object", "subtype": "html-element", "objectId": "oid-1"}
        if "el.checked = true" in expression:
            return {"type": "boolean", "value": True}
        if "el.checked = false" in expression:
            return {"type": "boolean", "value": False}
        if "return {selected" in expression:
            return {"type": "object", "value": {"selected": "b"}}
        if "el.focus()" in expression or "dispatchEvent" in expression:
            return {"type": "boolean", "value": True}
        if expression == "document.title":
            return {"type": "string", "value": "Fake Title"}
        return {"type": "undefined"}


@pytest.fixture
def fake_cdp():
    server = FakeCdpServer().start()
    yield server
    server.stop()


def _ws_client(timeout_s: float = 3.0) -> WebSocketClient:
    return WebSocketClient(timeout_s=timeout_s)


# -- RFC6455 client --------------------------------------------------------------------


def test_ws_text_roundtrip_with_echo(fake_cdp) -> None:
    client = _ws_client()
    client.connect(fake_cdp.cdp_url)
    client.send("hello ws")
    assert client.recv(timeout_s=3.0) == "hello ws"
    client.close()


def test_ws_ping_answered_with_pong(fake_cdp) -> None:
    client = _ws_client()
    client.connect(fake_cdp.cdp_url)
    client.send("PING-ME")
    assert client.recv(timeout_s=3.0) == "PING-ME"
    assert fake_cdp.last_pong == b"\x01\x02"
    client.close()


def test_ws_fragmented_server_message(fake_cdp) -> None:
    client = _ws_client()
    client.connect(fake_cdp.cdp_url)
    client.send("FRAG")
    assert client.recv(timeout_s=3.0) == "Hello CDP"
    client.close()


def test_ws_server_close_returns_none(fake_cdp) -> None:
    client = _ws_client()
    client.connect(fake_cdp.cdp_url)
    client.send("CLOSE")
    assert client.recv(timeout_s=3.0) is None
    assert client.closed


def test_ws_recv_timeout(fake_cdp) -> None:
    client = _ws_client()
    client.connect(fake_cdp.cdp_url)
    client.send("SLOW:wait")
    with pytest.raises(WsError) as exc_info:
        client.recv(timeout_s=0.3)
    assert exc_info.value.code == "WS_TIMEOUT"
    client.close()


def test_ws_rejects_bad_accept_key() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def bad_server():
        conn, _ = server.accept()
        with suppress(OSError):
            buffer = bytearray()
            while b"\r\n\r\n" not in bytes(buffer):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer.extend(chunk)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Sec-WebSocket-Accept: bogus-key\r\n"
                b"\r\n"
            )
            time.sleep(0.5)
            conn.close()

    thread = threading.Thread(target=bad_server, daemon=True)
    thread.start()
    client = _ws_client()
    with pytest.raises(WsError) as exc_info:
        client.connect(f"ws://127.0.0.1:{port}/devtools/browser/x")
    assert exc_info.value.code == "WS_PROTOCOL"
    thread.join(timeout=2.0)
    server.close()


# -- CDP session -------------------------------------------------------------------------


def _cdp_session(fake_cdp, timeout_s: float = 5.0) -> CdpSession:
    session = CdpSession(fake_cdp.cdp_url, timeout_s=timeout_s)
    session.start()
    return session


def test_cdp_send_receive(fake_cdp) -> None:
    session = _cdp_session(fake_cdp)
    result = session.send("Target.getTargets")
    ids = {info["targetId"] for info in result["targetInfos"]}
    assert {"t1", "t2", "sw1"} <= ids
    session.close()


def test_cdp_error_propagates(fake_cdp) -> None:
    session = _cdp_session(fake_cdp)
    with pytest.raises(CdpError) as exc_info:
        session.send("Target.bogus")
    assert exc_info.value.code == "CDP_ERROR"
    assert "Unknown method" in exc_info.value.message
    session.close()


def test_cdp_command_timeout(fake_cdp) -> None:
    session = _cdp_session(fake_cdp, timeout_s=20.0)
    with pytest.raises(CdpError) as exc_info:
        session.send("Target.slow", timeout_s=0.4)
    assert exc_info.value.code == "CDP_TIMEOUT"
    session.close()


def test_cdp_events_filter_and_requeue(fake_cdp) -> None:
    session = _cdp_session(fake_cdp)
    s1 = session.send("Target.attachToTarget", {"targetId": "t1", "flatten": True})["sessionId"]
    s2 = session.send("Target.attachToTarget", {"targetId": "t2", "flatten": True})["sessionId"]
    session.send("Runtime.enable", {}, session_id=s1)
    session.send("Network.enable", {}, session_id=s1)
    session.send("Runtime.enable", {}, session_id=s2)

    console = session.events(session_id=s1, methods={"Runtime.consoleAPICalled"}, limit=10)
    assert [e["method"] for e in console] == ["Runtime.consoleAPICalled"]
    assert console[0]["sessionId"] == s1

    network = session.events(session_id=s1, methods={"Network.requestWillBeSent"}, limit=10)
    assert len(network) == 1

    # s1's remaining events are consumed by an unfiltered session-limited drain,
    # s2's console event must survive the s1-filtered drains (re-queued).
    rest_s1 = session.events(session_id=s1, limit=50)
    assert rest_s1  # response events for t1 already consumed; at least network left
    s2_console = session.events(
        session_id=s2, methods={"Runtime.consoleAPICalled"}, limit=10, wait_s=2.0
    )
    assert len(s2_console) == 1
    session.close()


def test_cdp_disconnect_wakes_pending_waiters(fake_cdp) -> None:
    session = _cdp_session(fake_cdp, timeout_s=20.0)
    session.send("Target.getTargets")  # proves the link works

    def drop_later():
        time.sleep(0.3)
        fake_cdp.drop()

    threading.Thread(target=drop_later, daemon=True).start()
    with pytest.raises(CdpError) as exc_info:
        # A long command that would outlive the connection drop.
        session.send("Target.slow", timeout_s=3.0)
    assert exc_info.value.code == "CDP_DISCONNECTED"
    session.close()


# -- manager -------------------------------------------------------------------------------


def _manager(tmp_path: Path) -> BrowserManager:
    return BrowserManager(
        session_id="s-browser",
        home_root=tmp_path / "home",
    )


def test_manager_status_disconnected(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    status = manager.status()
    assert status["state"] == "disconnected"
    assert status["targets"] == 0


def test_manager_connect_and_tabs(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    status = manager.status()
    assert status["state"] == "connected"
    assert status["managed"] is False
    assert status["targets"] == 2
    tabs = manager.targets()
    assert {t["target_id"] for t in tabs} == {"t1", "t2"}  # service worker filtered
    manager.close()


def test_manager_connect_without_endpoint_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    with pytest.raises(BrowserError) as exc_info:
        manager.connect()
    assert exc_info.value.code == "BROWSER_DEPENDENCY"


def test_manager_new_page_and_close(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    created = manager.new_page("https://example.com/new")
    assert created["target_id"].startswith("t")
    assert any(t["target_id"] == created["target_id"] for t in manager.targets())
    manager.close_page(created["target_id"])
    assert not any(t["target_id"] == created["target_id"] for t in manager.targets())
    manager.close()


def test_manager_evaluate_ok_and_js_error(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    out = manager.evaluate("t1", "document.title")
    assert out["value"] == "Fake Title"
    with pytest.raises(BrowserError) as exc_info:
        manager.evaluate("t1", "globalThis.__boom__()")
    assert exc_info.value.code == "JS_ERROR"
    assert "boom" in exc_info.value.message
    manager.close()


def test_manager_navigate_and_error_text(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    out = manager.navigate("t1", "https://example.com/page")
    assert out["frame_id"] == "frame-0"
    assert fake_cdp.targets["t1"]["url"] == "https://example.com/page"
    with pytest.raises(BrowserError) as exc_info:
        manager.navigate("t1", "https://blocked.example/x")
    assert exc_info.value.code == "BROWSER_PROTOCOL"
    manager.close()


def test_manager_cancellation(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    with pytest.raises(BrowserError) as exc_info:
        manager.navigate("t1", "https://example.com/x", cancelled=lambda: True)
    assert exc_info.value.code == "CANCELLED"
    manager.close()


def test_manager_snapshot_and_truncation(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    server = FakeCdpServer(big_html=True).start()
    try:
        manager = _manager(tmp_path)
        manager.connect(server.base_url)
        snap = manager.snapshot("t1")
        assert snap["truncated"] is True
        assert len(snap["html"]) == 256 * 1024
        assert snap["bytes"] > 300_000
        manager.close()
    finally:
        server.stop()


def test_manager_click_resolves_selector(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    out = manager.click("t1", selector="#primary")
    assert out["clicked"] == {"x": 100.5, "y": 200.25}
    mouse = [(m, p) for (m, p, _sid) in fake_cdp.commands if m == "Input.dispatchMouseEvent"]
    types = [p["type"] for _, p in mouse]
    assert types == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert mouse[-1][1]["x"] == 100.5
    manager.close()


def test_manager_click_coordinates(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    out = manager.click("t1", x=7.5, y=8.5)
    assert out["clicked"] == {"x": 7.5, "y": 8.5}
    with pytest.raises(BrowserError) as exc_info:
        manager.click("t1", x=1.0)
    assert exc_info.value.code == "INVALID_ARGUMENT"
    manager.close()


def test_manager_fill_select_and_check(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    assert manager.fill("t1", "#user", "ada")["chars"] == 3
    assert manager.select_option("t1", "#country", "b")["selected"] == "b"
    assert manager.set_checked("t1", "#agree", True)["checked"] is True
    assert manager.set_checked("t1", "#agree", False)["checked"] is False
    manager.close()


def test_manager_screenshot_and_a11y(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    png = manager.screenshot("t1")
    assert png == base64.b64decode(_PNG)
    tree = manager.a11y_tree("t1")
    assert tree["total"] == 3
    assert tree["nodes"][0]["role"] == "RootWebArea"
    small = manager.a11y_tree("t1", max_nodes=2)
    assert small["truncated"] is True
    assert len(small["nodes"]) == 2
    manager.close()


def test_manager_console_and_network_events(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    console = manager.console_events("t1")
    assert console == [{"type": "log", "text": "hello from page 42"}]
    requests = manager.network_events("t1")
    assert requests == [
        {
            "url": "https://example.com/",
            "method": "GET",
            "type": "Document",
            "status": 200,
            "mimeType": "text/html",
        }
    ]
    manager.close()


def test_manager_cookies_redacted(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    cookies = manager.cookies("t1")
    assert cookies[0]["name"] == "session"
    assert cookies[0]["value"] == "***"
    assert "super-secret-cookie-value" not in json.dumps(cookies)
    manager.close()


def test_manager_upload_set_file_input(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    upload_file = tmp_path / "upload.txt"
    upload_file.write_text("data", encoding="utf-8")
    out = manager.set_file_input("t1", "input[type=file]", upload_file)
    assert out["file"] == str(upload_file)
    dom = [p for (m, p, _sid) in fake_cdp.commands if m == "DOM.setFileInputFiles"]
    assert dom[-1]["objectId"] == "oid-1"
    assert dom[-1]["files"] == [str(upload_file.resolve())]
    manager.close()


def test_manager_download_poll(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    fake_cdp.download_file = b"pdf-bytes-1234"
    directory = tmp_path / "downloads"
    manager.begin_download(directory)
    ref = manager.poll_download(timeout_s=5.0)
    assert ref is not None
    assert ref.suggested_name == "report.pdf"
    assert ref.bytes == len(b"pdf-bytes-1234")
    assert Path(ref.path).read_bytes() == b"pdf-bytes-1234"
    manager.close()


def test_manager_close_closes_session_only(tmp_path, monkeypatch, fake_cdp) -> None:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    closed = manager.close()
    assert closed == {"closed": ["cdp-session"]}
    assert manager.status()["state"] == "disconnected"


# -- tools via the real runtime ------------------------------------------------------------


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(browse_tools())
    return registry


def _ctx(
    tmp_path: Path, *, manager, network=None, profile=PermissionProfile.WORKSPACE
) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="s-browser-tool",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=profile,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        network=network,
        browser=manager,
    )


def _runtime(tmp_path: Path, *, manager, network_mode: str, answer: str = "n") -> ToolRuntime:
    policy = PolicyEngine(network=NetworkPolicy(mode=network_mode))
    return ToolRuntime(
        _tool_registry(),
        policy,
        ApprovalEngine(prompt=lambda req: answer),
        clock=FakeClock(),
    )


def _connected_manager(tmp_path: Path, fake_cdp, monkeypatch) -> BrowserManager:
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    manager = _manager(tmp_path)
    manager.connect(fake_cdp.base_url)
    return manager


def test_browse_tools_registered() -> None:
    names = {t.name for t in browse_tools()}
    expected = {
        "browser.status",
        "browser.launch",
        "browser.connect",
        "browser.close",
        "browser.tabs",
        "browser.tabs_close",
        "browser.open",
        "browser.navigate",
        "browser.snapshot",
        "browser.a11y",
        "browser.screenshot",
        "browser.click",
        "browser.fill",
        "browser.type",
        "browser.select",
        "browser.check",
        "browser.scroll",
        "browser.drag",
        "browser.evaluate",
        "browser.console",
        "browser.network",
        "browser.cookies",
        "browser.set_cookie",
        "browser.upload",
        "browser.download",
    }
    assert expected <= names


def test_browser_status_via_runtime(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    result = runtime.execute("browser.status", {}, ctx)
    assert result.ok, result.error
    assert result.data["state"] == "connected"
    assert result.data["targets"] == 2
    manager.close()


def test_browser_navigate_network_denied_by_policy(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="off")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="off")))
    result = runtime.execute("browser.navigate", {"url": "https://example.com/"}, ctx)
    assert not result.ok
    assert result.error.code.value == "POLICY_DENIED"
    manager.close()


def test_browser_navigate_allowed_end_to_end(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    result = runtime.execute("browser.navigate", {"url": "https://example.com/deep"}, ctx)
    assert result.ok, result.error
    assert result.data["url"] == "https://example.com/deep"
    manager.close()


def test_browser_click_requires_consent_once_per_session(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    prompts: list[str] = []
    policy = PolicyEngine(network=NetworkPolicy(mode="allow"))
    approvals = ApprovalEngine(prompt=lambda req: (prompts.append(req.capability), "s")[1])
    runtime = ToolRuntime(
        _tool_registry(),
        policy,
        approvals,
        clock=FakeClock(),
    )
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    first = runtime.execute("browser.click", {"selector": "#a"}, ctx)
    second = runtime.execute("browser.click", {"selector": "#b"}, ctx)
    assert first.ok, first.error
    assert second.ok, second.error
    assert [c for c in prompts if c == "browser.mutate"] == ["browser.mutate"]
    manager.close()


def test_browser_click_denied_for_read_only_profile(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow")
    ctx = _ctx(
        tmp_path,
        manager=manager,
        network=NetworkGuard(NetworkPolicy(mode="allow")),
        profile=PermissionProfile.READ_ONLY,
    )
    result = runtime.execute("browser.click", {"selector": "#a"}, ctx)
    assert not result.ok
    assert result.error.code.value == "POLICY_DENIED"
    manager.close()


def test_browser_navigate_about_blank_bypasses_network_gate(
    tmp_path, fake_cdp, monkeypatch
) -> None:
    # mode=off denies every network dial, but about:blank is a local page:
    # navigation must still be possible (consent via browser.mutate approval).
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="off", answer="s")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="off")))
    result = runtime.execute("browser.navigate", {"url": "about:blank"}, ctx)
    assert result.ok, result.error
    manager.close()


def test_browser_snapshot_truncated_spills_artifact(tmp_path, monkeypatch) -> None:
    server = FakeCdpServer(big_html=True).start()
    try:
        monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
        manager = _manager(tmp_path)
        manager.connect(server.base_url)
        runtime = _runtime(tmp_path, manager=manager, network_mode="allow")
        ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
        result = runtime.execute("browser.snapshot", {"target_id": "t1"}, ctx)
        assert result.ok, result.error
        assert result.data["truncated"] is True
        assert len(result.data["html"]) == 8000
        artifact = result.data["artifact"]
        assert artifact.startswith("file://")
        full_path = Path(artifact[len("file://") :])
        assert full_path.read_text(encoding="utf-8", errors="replace")
        assert result.artifacts
        manager.close()
    finally:
        server.stop()


def test_browser_screenshot_artifact(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    result = runtime.execute("browser.screenshot", {"target_id": "t1"}, ctx)
    assert result.ok, result.error
    assert result.data["sha256"] == hashlib.sha256(base64.b64decode(_PNG)).hexdigest()
    png_path = Path(result.data["artifact"][len("file://") :])
    assert png_path.exists()
    assert result.artifacts[0].kind == "screenshot"
    manager.close()


def test_browser_upload_requires_sandbox_and_provenance(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow", answer="s")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    denied = runtime.execute(
        "browser.upload",
        {"selector": "input[type=file]", "path": str(outside)},
        ctx,
    )
    assert not denied.ok
    assert denied.error.code.value == "SANDBOX_VIOLATION"

    inside = tmp_path / "work" / "data.txt"
    inside.write_text("upload payload", encoding="utf-8")
    allowed = runtime.execute(
        "browser.upload",
        {"selector": "input[type=file]", "path": str(inside)},
        ctx,
    )
    assert allowed.ok, allowed.error
    provenance = allowed.data["provenance"]
    assert provenance["bytes"] == len("upload payload")
    assert provenance["sha256"] == hashlib.sha256(b"upload payload").hexdigest()
    manager.close()


def test_browser_download_captures_provenance(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    fake_cdp.download_file = b"download-bytes"
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow", answer="s")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    result = runtime.execute("browser.download", {"wait_s": 5}, ctx)
    assert result.ok, result.error
    assert result.data["suggested_name"] == "report.pdf"
    assert result.data["bytes"] == len(b"download-bytes")
    assert result.data["sha256"] == hashlib.sha256(b"download-bytes").hexdigest()
    artifact_path = Path(result.data["artifact"][len("file://") :])
    assert artifact_path.read_bytes() == b"download-bytes"
    manager.close()


def test_browser_download_timeout_without_trigger(tmp_path, fake_cdp, monkeypatch) -> None:
    manager = _connected_manager(tmp_path, fake_cdp, monkeypatch)
    runtime = _runtime(tmp_path, manager=manager, network_mode="allow", answer="s")
    ctx = _ctx(tmp_path, manager=manager, network=NetworkGuard(NetworkPolicy(mode="allow")))
    result = runtime.execute("browser.download", {"wait_s": 1.5}, ctx)
    assert not result.ok
    assert result.error.code.value == "TIMEOUT"
    assert result.error.retryable
    manager.close()
