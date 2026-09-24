"""The desktop terminal: ConPTY on Windows (pywinpty), raw keystrokes, the
tail buffer that keeps streaming past 128 KB, shells and the default cwd."""

from __future__ import annotations

import itertools
import json
import sys
import time
from types import SimpleNamespace

import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.pty import EnginePtyService, available_shells
from rinari.engine_protocol.server import EngineServer
from rinari.tools.native.ptytools import PtyHandle, PtyRegistry, TailBuffer, _winpty

needs_conpty = pytest.mark.skipif(
    sys.platform != "win32" or _winpty() is None, reason="ConPTY needs Windows and pywinpty"
)


def test_the_tail_keeps_the_latest_bytes_by_absolute_offset() -> None:
    tail = TailBuffer(limit=10)
    tail.write(b"0123456789")
    data, end = tail.read_from(0)
    assert data == b"0123456789" and end == 10
    tail.write(b"abcde")  # 5 oldest bytes dropped
    data, end = tail.read_from(10)
    assert data == b"abcde" and end == 15
    # A reader that fell behind resumes at the oldest byte kept.
    data, _end = tail.read_from(0)
    assert data == b"56789abcde"
    assert tail.read_from(15) == (b"", 15)


class _FakeHandle(PtyHandle):
    def __init__(self) -> None:
        super().__init__("pty_001", "shell", "", None, SimpleNamespace(), backend="fake")
        self.sent: list[bytes] = []

    def write_bytes(self, payload: bytes) -> int:
        self.sent.append(payload)
        return len(payload)


def test_raw_writes_are_keystrokes_and_lines_get_a_newline() -> None:
    service = EnginePtyService(lambda payload: None)
    handle = _FakeHandle()
    service._registry._handles[handle.id] = handle
    service.write(handle.id, "a", raw=True)
    service.write(handle.id, "\x03", raw=True)
    service.write(handle.id, "ls")
    assert handle.sent == [b"a", b"\x03", b"ls\n"]


def test_a_chat_in_the_home_root_opens_the_terminal_in_documents(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    record = SimpleNamespace(current_cwd=str(home), project_root_snapshot=None)
    service = EnginePtyService(
        lambda payload: None, home=home, resolve_session=lambda session_id: record
    )
    started: dict = {}
    monkeypatch.setattr(PtyRegistry, "supported", True)
    monkeypatch.setattr(
        PtyRegistry,
        "start",
        lambda self, command, cwd, env, columns, rows: started.update(cwd=cwd) or "pty_009",
    )
    monkeypatch.setattr(service, "_forward", lambda pty_id: None)
    service.start("shell", session_id="ses_1")
    assert started["cwd"] == str((home / "Documents").resolve())
    # An explicit home root is still refused.
    with pytest.raises(Exception, match="HOME"):
        service.start("shell", cwd=str(home))


def test_shells_start_with_the_default() -> None:
    shells = available_shells()
    assert shells and all({"id", "label", "command"} <= set(shell) for shell in shells)
    if sys.platform == "win32":
        assert "cmd" in {shell["id"] for shell in shells}
        assert shells[0]["id"] in {"pwsh", "powershell"}


# -- real ConPTY over the protocol ---------------------------------------------------

_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"term-{next(_IDS)}", "method": method, "params": params or {}}
    return server.handle_line(json.dumps(line))


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _collect(server, pty_id, until, timeout=30.0):
    output: list[str] = []
    exit_payload = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        for frame in server.drain_events():
            payload = frame.get("payload", {})
            if payload.get("pty_id") != pty_id:
                continue
            if frame.get("event") == "pty.output":
                output.append(payload["data"])
            elif frame.get("event") == "pty.exit":
                exit_payload = payload
        if until("".join(output), exit_payload):
            break
        time.sleep(0.05)
    return "".join(output), exit_payload


@pytest.fixture
def server(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    engine = EngineServer(build_services(app_ctx, user_home=home), user_home=home)
    yield engine
    engine.close()


@needs_conpty
def test_conpty_runs_a_command_and_reports_its_exit_code(server, tmp_path) -> None:
    shells = _ok(_call(server, "pty.shells"))
    assert shells["supported"] is True
    pty_id = _ok(
        _call(
            server,
            "pty.start",
            {"command": "cmd.exe /c echo hola-conpty && exit 3", "cwd": str(tmp_path)},
        )
    )["pty_id"]
    text, exited = _collect(server, pty_id, lambda text, exit_: exit_ is not None)
    assert "hola-conpty" in text
    assert exited is not None and exited["exit_code"] == 3
    # A reattaching terminal repaints from pty.read and skips events up to its offset.
    repaint = _ok(_call(server, "pty.read", {"pty_id": pty_id}))
    assert "hola-conpty" in repaint["data"]
    assert repaint["offset"] == len(repaint["data"].encode("utf-8"))


@needs_conpty
def test_an_interactive_powershell_gets_keystrokes_resize_and_stop(server, tmp_path) -> None:
    pty_id = _ok(
        _call(
            server,
            "pty.start",
            {"command": "powershell.exe -NoLogo -NoProfile", "cwd": str(tmp_path), "columns": 100},
        )
    )["pty_id"]
    time.sleep(1.0)
    _ok(
        _call(
            server,
            "pty.write",
            {"pty_id": pty_id, "data": "Write-Output ('suma=' + (2+3))\r", "raw": True},
        )
    )
    text, _exit = _collect(server, pty_id, lambda text, exit_: "suma=5" in text)
    assert "suma=5" in text
    _ok(_call(server, "pty.resize", {"pty_id": pty_id, "columns": 120, "rows": 40}))
    stopped = _ok(_call(server, "pty.terminate", {"pty_id": pty_id}))
    assert stopped["alive"] is False


@needs_conpty
def test_output_keeps_streaming_past_the_old_128_kb_cap(server, tmp_path) -> None:
    # ~260 KB: the head-only buffer used to stop streaming at 128 KB.
    loop = "for /L %i in (1,1,4000) do @echo line-%i-" + "x" * 50
    pty_id = _ok(
        _call(server, "pty.start", {"command": f'cmd.exe /c "{loop}"', "cwd": str(tmp_path)})
    )["pty_id"]
    text, exited = _collect(server, pty_id, lambda text, exit_: exit_ is not None, timeout=90)
    assert exited is not None
    assert "line-4000-" in text


@needs_conpty
def test_the_default_shell_opens_without_a_command(server, tmp_path) -> None:
    # The default may be PowerShell 7 under "C:\Program Files": a quoted path.
    pty_id = _ok(_call(server, "pty.start", {"cwd": str(tmp_path)}))["pty_id"]
    time.sleep(1.5)
    _ok(_call(server, "pty.write", {"pty_id": pty_id, "data": "exit\r", "raw": True}))
    _text, exited = _collect(server, pty_id, lambda text, exit_: exit_ is not None)
    assert exited is not None and exited["exit_code"] == 0
