"""wait.for: waits on a real condition (localhost sockets, files, process output)."""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
from dataclasses import replace

import pytest

from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError
from rinari.tools.catalog import builtin_catalog
from rinari.tools.definition import ToolErrorCode
from rinari.tools.native.process import ProcessRegistry, process_start
from rinari.tools.native.wait import wait_for
from tests.unit.test_tool_runtime import _ctx, _runtime


@pytest.fixture
def ctx(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return replace(_ctx(tmp_path, root), processes=ProcessRegistry())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listen(port: int, delay: float = 0.0) -> socket.socket:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def open_later():
        time.sleep(delay)
        server.bind(("127.0.0.1", port))
        server.listen()

    if delay:
        threading.Thread(target=open_later, daemon=True).start()
    else:
        open_later()
    return server


class _Status(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _http_server(status: int):
    handler = type("Handler", (_Status,), {"status": status})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_an_open_port_is_ready_at_once(ctx):
    port = _free_port()
    server = _listen(port)
    try:
        result = wait_for({"port": port, "host": "127.0.0.1", "timeout_s": 5}, ctx)
    finally:
        server.close()
    assert result.ok and result.data["ready"] is True
    assert result.data["state"] == "ready" and result.data["checks"] == 1
    assert result.data["target"] == f"127.0.0.1:{port}"


def test_waiting_returns_as_soon_as_the_server_starts_listening(ctx):
    port = _free_port()
    server = _listen(port, delay=0.6)
    try:
        started = time.monotonic()
        result = wait_for(
            {"port": port, "host": "127.0.0.1", "timeout_s": 20, "interval_s": 0.1}, ctx
        )
        elapsed = time.monotonic() - started
    finally:
        server.close()
    # Windows retries a refused localhost connect for ~2 s, so one probe may span
    # the moment the server opens: what matters is that it did not sleep blindly.
    assert result.data["state"] == "ready"
    assert 0.5 <= elapsed < 10


def test_a_port_that_never_opens_times_out_with_the_last_error(ctx):
    result = wait_for({"port": _free_port(), "host": "127.0.0.1", "timeout_s": 0.5}, ctx)
    assert result.ok and result.data["ready"] is False
    assert result.data["state"] == "timed_out"
    assert result.data["last"]["error"]
    assert result.data["timeout"]["effective_s"] == 0.5


def test_a_url_is_ready_when_it_answers(ctx):
    server = _http_server(200)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        result = wait_for({"url": url, "timeout_s": 10}, ctx)
    finally:
        server.shutdown()
    assert result.data["state"] == "ready" and result.data["last"] == {"status": 200}


def test_a_url_outside_the_expected_status_range_is_not_ready(ctx):
    server = _http_server(503)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        result = wait_for({"url": url, "timeout_s": 0.6, "interval_s": 0.2}, ctx)
    finally:
        server.shutdown()
    assert result.data["state"] == "timed_out" and result.data["last"] == {"status": 503}


def test_text_in_a_process_output_is_awaited(ctx):
    started = process_start(
        {
            "argv": _python(
                "import time; time.sleep(0.5); print('Listening on 8080', flush=True); "
                "time.sleep(30)"
            )
        },
        ctx,
    )
    handle = started.data["handle"]
    try:
        result = wait_for({"output": "Listening on", "handle": handle, "timeout_s": 20}, ctx)
    finally:
        ctx.processes.kill(ctx.processes.get(handle))
    assert result.data["state"] == "ready"
    assert result.data["last"]["line"] == "Listening on 8080"


def test_a_regex_matches_process_output(ctx):
    started = process_start({"argv": _python("print('ready in 42 ms')")}, ctx)
    result = wait_for(
        {"output": r"ready in \d+ ms", "regex": True, "handle": started.data["handle"]}, ctx
    )
    assert result.data["state"] == "ready"


def test_a_process_that_exits_first_ends_the_wait_with_its_output(ctx):
    started = process_start({"argv": _python("import sys; print('port in use'); sys.exit(3)")}, ctx)
    result = wait_for(
        {"output": "Listening", "handle": started.data["handle"], "timeout_s": 20}, ctx
    )
    assert result.data["state"] == "exited" and result.data["ready"] is False
    assert result.data["process"]["exit_code"] == 3
    assert "port in use" in result.data["process"]["output_tail"]


def test_a_watched_process_stops_a_port_wait_when_it_dies(ctx):
    started = process_start({"argv": _python("raise SystemExit(1)")}, ctx)
    result = wait_for(
        {
            "port": _free_port(),
            "host": "127.0.0.1",
            "handle": started.data["handle"],
            "timeout_s": 20,
        },
        ctx,
    )
    assert result.data["state"] == "exited" and result.data["process"]["exit_code"] == 1


def test_a_file_is_awaited_until_it_exists(ctx):
    target = ctx.cwd / "build" / "done.txt"

    def create():
        time.sleep(0.4)
        target.parent.mkdir()
        target.write_text("ok", encoding="utf-8")

    threading.Thread(target=create, daemon=True).start()
    result = wait_for({"file": "build/done.txt", "timeout_s": 10, "interval_s": 0.1}, ctx)
    assert result.data["state"] == "ready" and result.data["last"]["size_bytes"] == 2


def test_cancelling_the_turn_interrupts_the_wait_promptly(ctx):
    token = CancellationToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(CancelledError):
        wait_for(
            {"file": "never.txt", "timeout_s": 60, "interval_s": 10},
            replace(ctx, cancellation=token),
        )
    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({}, "exactly one condition"),
        ({"port": 80, "url": "http://localhost/"}, "received port, url"),
        ({"output": "x"}, "needs handle"),
        ({"file": "a", "host": "x"}, "host only applies"),
        ({"url": "ftp://x"}, "http(s) URL"),
        ({"output": "(", "regex": True, "handle": "proc_001"}, "valid regex"),
        ({"url": "http://localhost/", "status_min": 500, "status_max": 200}, "status_min"),
    ],
)
def test_malformed_conditions_are_rejected_before_policy(ctx, tmp_path, arguments, fragment):
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute("wait.for", arguments, ctx)
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert fragment in result.error.message


def test_policy_sees_each_resource_like_the_tool_that_would_check_it():
    tool = builtin_catalog().get("wait.for")
    port = tool.classify_actions({"port": 3000})
    url = tool.classify_actions({"url": "https://example.com/"})
    file = tool.classify_actions({"file": "out/log.txt"})
    output = tool.classify_actions({"output": "ready", "handle": "proc_001"})
    assert [(a.capability, a.target, a.mode) for a in port] == [
        ("network.outbound", "localhost:3000", "read")
    ]
    assert [(a.capability, a.mode) for a in url] == [("network.outbound", "read")]
    assert [(a.capability, a.target) for a in file] == [("fs.read", "out/log.txt")]
    assert [a.capability for a in output] == ["process.local"]
    assert tool.always_loaded and tool.namespace == "wait"


def test_a_localhost_wait_runs_without_approval(ctx, tmp_path):
    runtime, _ = _runtime(ctx, tmp_path, answer="n")
    port = _free_port()
    server = _listen(port)
    try:
        result = runtime.execute(
            "wait.for", {"port": port, "host": "127.0.0.1", "timeout_s": 5}, ctx
        )
    finally:
        server.close()
    assert result.ok and result.data["ready"] is True


def test_the_shell_points_at_wait_for_instead_of_sleeping():
    catalog = builtin_catalog()
    assert "wait.for" in catalog.get("shell.exec").description
    assert "Start-Sleep" in catalog.get("wait.for").description
