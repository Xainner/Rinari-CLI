"""Remote MCP servers: Streamable HTTP transport, auth, secrets and diagnosis.

A real HTTP server runs in-process on 127.0.0.1 (no external network). It
speaks the 2025-03-26 Streamable HTTP transport in two flavours (JSON body
and SSE stream), checks a bearer token, issues an `Mcp-Session-Id` and can be
told to stall, to answer 404/405 or to expire the session.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.mcp.client import McpClient, McpError
from rinari.mcp.config import McpConfigError
from rinari.mcp.http_transport import StreamableHttpTransport, safe_url

TOKEN = "tok-very-secret-123"
OTHER_TOKEN = "tok-rotated-456"
HEADER_SECRET = "hdr-secret-789"


class FakeRemote:
    """Scripted Streamable HTTP MCP server state shared with the handler."""

    def __init__(self) -> None:
        self.mode = "json"  # json | sse
        self.token: str | None = TOKEN
        self.required_header: tuple[str, str] | None = None
        self.stall_s = 0.0
        self.expire_next = False
        self.session = "sess-1"
        self.requests: list[dict[str, Any]] = []
        self.deletes: list[dict[str, str]] = []
        self.pings_answered = 0
        self.lock = threading.Lock()

    def reply(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, rid = msg.get("method"), msg.get("id")
        if rid is not None and method is None:
            # A response from the client (answer to our ping).
            self.pings_answered += 1
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "remote-fake", "version": "1.2.3"},
                    "capabilities": {"tools": {}, "resources": {}},
                },
            }
        if method and method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "tools": [
                        {"name": "echo", "inputSchema": {"type": "object"}},
                        {"name": "sum", "inputSchema": {"type": "object"}},
                    ]
                },
            }
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"resources": [{"uri": "r://1"}]}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}}


def _handler(remote: FakeRemote):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep test output quiet
            return

        def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_DELETE(self) -> None:
            remote.deletes.append(dict(self.headers))
            self._send(200)

        def do_GET(self) -> None:
            self._send(405)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length) or b"{}")
            with remote.lock:
                remote.requests.append({"headers": dict(self.headers), "body": msg})
            if self.path == "/legacy":
                self._send(405)
                return
            if self.path != "/mcp":
                self._send(404, b"not here", {"Content-Type": "text/plain"})
                return
            if remote.token is not None and self.headers.get("Authorization") != (
                f"Bearer {remote.token}"
            ):
                self._send(
                    401,
                    b'{"error":"unauthorized"}',
                    {"Content-Type": "application/json", "WWW-Authenticate": "Bearer"},
                )
                return
            if remote.required_header is not None:
                name, value = remote.required_header
                if self.headers.get(name) != value:
                    self._send(403)
                    return
            if msg.get("method") != "initialize":
                if remote.expire_next:
                    remote.expire_next = False
                    self._send(404)
                    return
                if self.headers.get("Mcp-Session-Id") != remote.session:
                    self._send(400, b"missing session")
                    return
            if remote.stall_s:
                time.sleep(remote.stall_s)
            answer = remote.reply(msg)
            headers = {}
            if msg.get("method") == "initialize":
                headers["Mcp-Session-Id"] = remote.session
            if answer is None:
                self._send(202, headers=headers)
                return
            if remote.mode == "sse":
                frames = [
                    # A progress notification and a server ping come first.
                    {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                    {"jsonrpc": "2.0", "id": "srv-ping-1", "method": "ping"},
                    answer,
                ]
                body = "".join(
                    f"event: message\nid: {i}\ndata: {json.dumps(f)}\n\n"
                    for i, f in enumerate(frames)
                ).encode("utf-8")
                headers["Content-Type"] = "text/event-stream"
                self._send(200, b": keep-alive\n\n" + body, headers)
                return
            headers["Content-Type"] = "application/json"
            self._send(200, json.dumps(answer).encode("utf-8"), headers)

    return Handler


@pytest.fixture
def remote():
    state = FakeRemote()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}/mcp"  # type: ignore[attr-defined]
    state.base = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield state
    server.shutdown()
    server.server_close()


def _client(url: str, token: str | None = TOKEN, timeout_s: float = 5.0) -> McpClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return McpClient(StreamableHttpTransport(url, headers=headers, timeout_s=timeout_s), timeout_s)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_json_response_session_and_protocol_headers(remote) -> None:
    client = _client(remote.url)
    info = client.connect()
    assert info == {"name": "remote-fake", "version": "1.2.3"}
    assert client.protocol_version == "2025-03-26"
    tools = client.list_tools()
    assert [t.name for t in tools] == ["echo", "sum"]
    init = remote.requests[0]
    assert init["body"]["method"] == "initialize"
    assert init["body"]["params"]["protocolVersion"] == "2025-03-26"
    assert "application/json" in init["headers"]["Accept"]
    assert "text/event-stream" in init["headers"]["Accept"]
    assert "Mcp-Session-Id" not in init["headers"]
    later = remote.requests[-1]["headers"]
    assert later["Mcp-Session-Id"] == "sess-1"
    assert later["MCP-Protocol-Version"] == "2025-03-26"
    client.close()
    assert len(remote.deletes) == 1
    assert remote.deletes[0]["Mcp-Session-Id"] == "sess-1"


def test_sse_response_skips_notifications_and_answers_ping(remote) -> None:
    remote.mode = "sse"
    client = _client(remote.url)
    client.connect()
    assert [t.name for t in client.list_tools()] == ["echo", "sum"]
    assert remote.pings_answered >= 1
    client.close()


def test_unauthorized_maps_to_auth_rejected(remote) -> None:
    client = _client(remote.url, token="wrong")
    with pytest.raises(McpError) as excinfo:
        client.connect()
    assert excinfo.value.code == "MCP_AUTH_REJECTED"
    assert excinfo.value.http_status == 401
    assert excinfo.value.hint == "check_credentials"
    assert "wrong" not in excinfo.value.message


def test_timeout_maps_to_mcp_timeout(remote) -> None:
    remote.stall_s = 1.5
    client = _client(remote.url, timeout_s=0.5)
    with pytest.raises(McpError) as excinfo:
        client.connect()
    assert excinfo.value.code == "MCP_TIMEOUT"
    assert excinfo.value.retryable is True


def test_wrong_path_and_legacy_endpoint(remote) -> None:
    with pytest.raises(McpError) as excinfo:
        _client(remote.base + "/other").connect()
    assert (excinfo.value.code, excinfo.value.http_status) == ("MCP_NOT_FOUND", 404)
    assert excinfo.value.hint == "check_endpoint_path"
    with pytest.raises(McpError) as excinfo:
        _client(remote.base + "/legacy").connect()
    assert excinfo.value.code == "MCP_TRANSPORT_UNSUPPORTED"
    assert excinfo.value.hint == "legacy_sse_unsupported"


def test_unreachable_host() -> None:
    client = _client(f"http://127.0.0.1:{_free_port()}/mcp", timeout_s=2.0)
    with pytest.raises(McpError) as excinfo:
        client.connect()
    assert excinfo.value.code == "MCP_UNREACHABLE"
    assert excinfo.value.hint == "check_url_or_network"


def test_expired_session_disconnects_client(remote) -> None:
    client = _client(remote.url)
    client.connect()
    remote.expire_next = True
    with pytest.raises(McpError) as excinfo:
        client.list_tools()
    assert excinfo.value.code == "MCP_SESSION_EXPIRED"
    assert client.connected is False


def test_safe_url_drops_credentials_and_query() -> None:
    assert safe_url("https://user:pw@h.example:8443/mcp?key=abc#x") == "https://h.example:8443/mcp"


# ---------------------------------------------------------------------------
# Service: secrets, update, test, probe
# ---------------------------------------------------------------------------


@pytest.fixture
def services(app_ctx, tmp_path):
    return build_services(app_ctx, user_home=tmp_path / "home")


def _secret_files(app_ctx) -> list:
    root = app_ctx.layout.credentials_dir / "mcp"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_bearer_token_is_stored_outside_the_row(services, app_ctx, remote) -> None:
    row = services.mcp.add("remote", url=remote.url, auth={"kind": "bearer", "token": TOKEN})
    assert row["transport"] == "http"
    assert row["url"] == remote.url
    assert TOKEN not in json.dumps(row)
    files = _secret_files(app_ctx)
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == TOKEN
    view = services.mcp.view(row)
    assert view["auth"] == {
        "kind": "bearer",
        "token": {"configured": True, "source": "stored"},
    }
    assert TOKEN not in json.dumps(view)
    result = services.mcp.test("remote")
    assert result["ok"] is True, result
    assert result["tools"] == 2
    assert result["resources"] == 1
    assert result["prompts"] is None  # not advertised by the server
    assert result["server_info"] == {"name": "remote-fake", "version": "1.2.3"}
    assert isinstance(result["latency_ms"], int)
    assert services.mcp.tools("remote")[0].name == "mcp.remote.echo"


def test_update_replaces_and_clears_secret(services, app_ctx, remote) -> None:
    services.mcp.add("remote", url=remote.url, auth={"kind": "bearer", "token": TOKEN})
    services.mcp.update("remote", {"auth": {"kind": "bearer", "token": OTHER_TOKEN}})
    files = _secret_files(app_ctx)
    assert [f.read_text(encoding="utf-8") for f in files] == [OTHER_TOKEN]
    failed = services.mcp.test("remote")
    assert (failed["ok"], failed["code"], failed["http_status"]) == (
        False,
        "MCP_AUTH_REJECTED",
        401,
    )
    assert failed["hint"] == "check_credentials"
    remote.token = OTHER_TOKEN
    assert services.mcp.test("remote")["ok"] is True
    # Keeping the token while editing something else.
    services.mcp.update("remote", {"auth": {"kind": "bearer"}, "timeout_s": 12})
    assert services.mcp.test("remote")["ok"] is True
    # Clearing: auth none deletes the stored secret.
    row = services.mcp.update("remote", {"auth": {"kind": "none"}})
    assert _secret_files(app_ctx) == []
    assert services.mcp.view(row)["auth"] == {"kind": "none"}
    assert services.mcp.view(row)["timeout_s"] == 12


def test_env_reference_and_secret_headers(services, app_ctx, remote, monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", TOKEN)
    remote.required_header = ("X-Api-Key", HEADER_SECRET)
    row = services.mcp.add(
        "remote",
        url=remote.url,
        auth={"kind": "bearer", "token": "env://MCP_TEST_TOKEN"},
        headers={"X-Api-Key": HEADER_SECRET, "X-Team": {"value": "core", "secret": False}},
    )
    services.mcp._env = {"MCP_TEST_TOKEN": TOKEN}
    view = services.mcp.view(row)
    assert view["auth"]["token"] == {
        "configured": True,
        "source": "env",
        "env_var": "MCP_TEST_TOKEN",
    }
    headers = {h["name"]: h for h in view["headers"]}
    # Sensitive-looking names default to secret; plain headers show the value.
    assert headers["X-Api-Key"] == {
        "name": "X-Api-Key",
        "secret": True,
        "configured": True,
        "source": "stored",
    }
    assert headers["X-Team"]["value"] == "core"
    assert [f.read_text(encoding="utf-8") for f in _secret_files(app_ctx)] == [HEADER_SECRET]
    assert services.mcp.test("remote")["ok"] is True
    # Removing the header (null) retires its stored secret.
    services.mcp.update("remote", {"headers": {"X-Api-Key": None}})
    assert _secret_files(app_ctx) == []
    assert services.mcp.test("remote")["code"] == "MCP_AUTH_REJECTED"  # 403


def test_missing_env_secret_is_reported(services, remote) -> None:
    services.mcp.add("remote", url=remote.url, auth={"kind": "bearer", "token": "env://NOPE_X"})
    services.mcp._credentials._env = {}
    result = services.mcp.test("remote")
    assert (result["ok"], result["code"], result["hint"]) == (
        False,
        "MCP_SECRET_MISSING",
        "secret_missing",
    )


def test_probe_never_stores_and_can_patch_a_saved_server(services, app_ctx, remote) -> None:
    result = services.mcp.probe({"url": remote.url, "auth": {"kind": "bearer", "token": TOKEN}})
    assert result["ok"] is True and result["server"] is None
    assert _secret_files(app_ctx) == []
    assert services.mcp.list() == []
    # Patch over a saved server: the stored token is reused, the URL changes.
    services.mcp.add("remote", url=remote.base + "/wrong", auth={"kind": "bearer", "token": TOKEN})
    assert services.mcp.test("remote")["code"] == "MCP_NOT_FOUND"
    patched = services.mcp.probe({"url": remote.url}, name="remote")
    assert patched["ok"] is True
    assert services.mcp.show("remote")["url"] == remote.base + "/wrong"  # unchanged


def test_remove_deletes_owned_secrets(services, app_ctx, remote) -> None:
    services.mcp.add("remote", url=remote.url, auth={"kind": "bearer", "token": TOKEN})
    assert _secret_files(app_ctx)
    assert services.mcp.remove("remote") is True
    assert _secret_files(app_ctx) == []


def test_stdio_env_values_are_stored_as_secrets(services, app_ctx) -> None:
    row = services.mcp.add(
        "local",
        ["my server", "--flag"],
        env={"API_KEY": "literal-env-secret", "MODE": "env://SOME_VAR"},
    )
    config = json.loads(row["config_json"])
    assert config["argv"] == ["my server", "--flag"]  # spaces inside args survive
    assert config["env"]["MODE"] == "env://SOME_VAR"
    assert config["env"]["API_KEY"].startswith("file://mcp/")
    assert "literal-env-secret" not in row["config_json"]
    names = {e["name"]: e for e in services.mcp.view(row)["env"]}
    assert names["API_KEY"] == {"name": "API_KEY", "configured": True, "source": "stored"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "ftp://example.com/mcp"},
        {"url": "https://user:pw@example.com/mcp"},
        {"url": "https://example.com/mcp", "auth": {"kind": "bearer"}},
        {"url": "https://example.com/mcp", "auth": {"kind": "bearer", "token": "keyring://x"}},
        {"url": "https://example.com/mcp", "headers": {"Mcp-Session-Id": "x"}},
        {"url": "https://example.com/mcp", "auth": {"kind": "oauth"}},
    ],
)
def test_invalid_remote_configs_are_rejected(services, kwargs) -> None:
    with pytest.raises(McpConfigError) as excinfo:
        services.mcp.add("bad", **kwargs)
    assert "pw" not in str(excinfo.value)


def test_secrets_never_reach_logs(services, remote, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    services.mcp.add("remote", url=remote.url, auth={"kind": "bearer", "token": TOKEN})
    services.mcp.test("remote")
    remote.token = OTHER_TOKEN
    services.mcp.test("remote")
    services.mcp.disconnect("remote")
    with pytest.raises(McpError):
        services.mcp.connect("remote")
    text = json.dumps(services.mcp.logs()) + caplog.text
    assert TOKEN not in text and OTHER_TOKEN not in text


# ---------------------------------------------------------------------------
# Protocol envelopes
# ---------------------------------------------------------------------------


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = itertools.count(1)


def _call(server, method: str, params: dict) -> dict:
    request = {"id": f"r{next(_ids)}", "method": method, "params": params}
    response = server.handle_line(json.dumps(request))
    assert response is not None
    text = json.dumps(response)
    assert TOKEN not in text and OTHER_TOKEN not in text and HEADER_SECRET not in text
    return response


def test_protocol_remote_crud_probe_and_test(server, remote) -> None:
    assert CAPABILITIES["mcp_remote_v1"] is True
    created = _call(
        server,
        "mcp.create",
        {
            "name": "remote",
            "transport": "http",
            "url": remote.url,
            "auth": {"kind": "bearer", "token": TOKEN},
            "headers": {"X-Client": "rinari"},
        },
    )
    assert created["ok"] is True, created
    view = created["result"]["server"]
    assert view["transport"] == "http"
    assert view["url"] == remote.url
    assert view["auth"]["token"]["configured"] is True
    assert view["headers"] == [
        {"name": "X-Client", "secret": False, "configured": True, "value": "rinari"}
    ]
    listed_response = _call(server, "mcp.list", {})
    assert listed_response["ok"] is True, listed_response
    listed = listed_response["result"]["servers"]
    assert listed[0]["auth"]["kind"] == "bearer"

    test = _call(server, "mcp.test", {"name": "remote"})["result"]["test"]
    assert test["ok"] is True and test["tools"] == 2

    updated = _call(
        server,
        "mcp.update",
        {"name": "remote", "auth": {"kind": "bearer", "token": OTHER_TOKEN}},
    )
    assert updated["ok"] is True
    failing = _call(server, "mcp.test", {"name": "remote"})["result"]["test"]
    assert (failing["code"], failing["http_status"], failing["hint"]) == (
        "MCP_AUTH_REJECTED",
        401,
        "check_credentials",
    )

    probe = _call(
        server, "mcp.probe", {"url": remote.url, "auth": {"kind": "bearer", "token": TOKEN}}
    )
    assert probe["result"]["test"]["ok"] is True
    patch_probe = _call(
        server, "mcp.probe", {"name": "remote", "auth": {"kind": "bearer", "token": TOKEN}}
    )
    assert patch_probe["result"]["test"]["ok"] is True

    bad = _call(server, "mcp.update", {"name": "remote", "url": "file:///etc/passwd"})
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"
    missing = _call(server, "mcp.update", {"name": "ghost", "timeout_s": 5})
    assert missing["error"]["code"] == "NOT_FOUND"
    empty = _call(server, "mcp.update", {"name": "remote"})
    assert empty["error"]["code"] == "INVALID_PARAMS"
    ghost_probe = _call(server, "mcp.probe", {"name": "ghost", "url": remote.url})
    assert ghost_probe["error"]["code"] == "NOT_FOUND"
    leak = _call(
        server,
        "mcp.create",
        {
            "name": "steal",
            "url": remote.url,
            "headers": {"X-Key": {"value": "keyring://providers/x", "secret": True}},
        },
    )
    assert leak["error"]["code"] == "INVALID_PARAMS"
