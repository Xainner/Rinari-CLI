"""Processes the model leaves running (plan 0.4.0 #90): reuse, cap, logs, stop,
notices, the durable ledger and, on Windows, Job Objects."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from rinari.engine_protocol.process_ledger import ProcessLedger
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.process import (
    MAX_LIVE_PROCESSES,
    ProcessRegistry,
    process_list,
    process_logs,
    process_start,
    process_stop,
    processes_note,
)

PY = sys.executable


def _ctx(tmp_path: Path, registry: ProcessRegistry) -> ToolContext:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    return ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile="workspace",
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=0.01),
        processes=registry,
    )


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{PY}" "{path}"'


SERVER = "import time\nprint('listo para servir', flush=True)\ntime.sleep(60)\n"


@pytest.fixture
def registry():
    created = ProcessRegistry()
    yield created
    created.stop_all(include_persistent=True)


def test_ready_when_a_line_and_the_same_command_is_reused(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    command = _script(tmp_path, "server.py", SERVER)
    started = process_start(
        {"command": command, "name": "dev", "ready_when": {"line": "listo", "timeout_s": 20}}, ctx
    )
    assert started.ok, started.error
    assert started.data["ready"] is True
    assert started.data["name"] == "dev"
    assert any("listo para servir" in line for line in started.data["last_lines"])
    again = process_start({"command": command, "name": "dev"}, ctx)
    assert again.ok and again.data["reused"] is True
    assert again.data["handle"] == started.data["handle"]
    listed = process_list({}, ctx).data["processes"]
    assert len(listed) == 1 and listed[0]["name"] == "dev" and listed[0]["ready"] is True


def test_at_most_eight_live_processes(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    for index in range(MAX_LIVE_PROCESSES):
        assert process_start({"command": _script(tmp_path, f"s{index}.py", SERVER)}, ctx).ok
    refused = process_start({"command": _script(tmp_path, "one-more.py", SERVER)}, ctx)
    assert not refused.ok
    assert "process.stop" in refused.error.message


def test_logs_show_the_end_of_a_chatty_process_and_grep(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    body = (
        "import sys\n"
        "for i in range(12000): print('ruido ' + 'x' * 60)\n"
        "print('ERROR: se cayo la base', flush=True)\n"
    )
    started = process_start({"command": _script(tmp_path, "chatty.py", body), "name": "w"}, ctx)
    registry.wait(registry.get(started.data["handle"]), 30)
    logs = process_logs({"name": "w", "lines": 3}, ctx)
    assert logs.ok
    assert logs.data["lines"][-1] == "ERROR: se cayo la base"
    only = process_logs({"name": "w", "grep": "error"}, ctx).data["lines"]
    assert only == ["ERROR: se cayo la base"]


def test_stop_by_name_and_the_model_hears_about_failures(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    server = process_start({"command": _script(tmp_path, "srv.py", SERVER), "name": "dev"}, ctx)
    failing = process_start(
        {
            "command": _script(
                tmp_path, "boom.py", "print('fallo la compilacion')\nraise SystemExit(2)\n"
            )
        },
        ctx,
    )
    registry.wait(registry.get(failing.data["handle"]), 20)
    stopped = process_stop({"name": "dev"}, ctx)
    assert stopped.ok and stopped.data["running"] is False
    ended = registry.take_exit_notices()
    # The failure is told once; a process the model stopped itself is not.
    assert [handle.id for handle in ended] == [failing.data["handle"]]
    note = processes_note(registry, ended)
    assert "exited with code 2" in note and "fallo la compilacion" in note
    assert registry.take_exit_notices() == []
    assert server.data["handle"] not in note


def test_a_port_served_by_someone_else_is_named_not_stolen(tmp_path, registry) -> None:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        ctx = _ctx(tmp_path, registry)
        refused = process_start(
            {"command": _script(tmp_path, "srv.py", SERVER), "ready_when": {"port": port}}, ctx
        )
        assert not refused.ok and f"Port {port} is already in use" in refused.error.message
        assert not process_stop({"port": port}, ctx).ok
    finally:
        listener.close()


def test_the_ledger_finds_orphans_of_an_earlier_engine(tmp_path) -> None:
    first = ProcessLedger(tmp_path)
    registry = ProcessRegistry(session_id="ses_1", ledger=first)
    ctx = _ctx(tmp_path, registry)
    started = process_start({"command": _script(tmp_path, "srv.py", SERVER), "name": "dev"}, ctx)
    ended = process_start({"command": _script(tmp_path, "quick.py", "print(1)\n")}, ctx)
    registry.wait(registry.get(ended.data["handle"]), 20)
    try:
        # A new Engine sees the one still running, and not the one that ended.
        second = ProcessLedger(tmp_path)
        orphans = second.orphans()
        assert [(o["handle"], o["name"]) for o in orphans] == [(started.data["handle"], "dev")]
        assert second.stop_orphan(orphans[0]["id"]) is True
        deadline = time.monotonic() + 10
        while (
            registry.get(started.data["handle"]).exit_code is None and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        assert registry.get(started.data["handle"]).exit_code is not None
        assert second.orphans() == []
    finally:
        registry.stop_all(include_persistent=True)


def test_stop_all_keeps_what_the_model_asked_to_keep(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    normal = process_start({"command": _script(tmp_path, "a.py", SERVER)}, ctx)
    kept = process_start({"command": _script(tmp_path, "b.py", SERVER), "persist": True}, ctx)
    assert registry.stop_all() == [normal.data["handle"]]
    assert registry.get(kept.data["handle"]).exit_code is None


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_stopping_ends_what_it_launched_with_start(tmp_path, registry) -> None:
    ctx = _ctx(tmp_path, registry)
    marker = f"rinari-job-{time.monotonic_ns()}"
    child = _script(tmp_path, "child.py", "import time\ntime.sleep(60)\n")
    started = process_start(
        {"command": f'cmd /c start "" /b {child} {marker} & ping -n 60 127.0.0.1 > nul'}, ctx
    )
    assert started.ok
    time.sleep(2)

    def children() -> int:
        query = (
            "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
            f"'*{marker}*' -and $_.Name -eq 'python.exe' }}).Count"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", query],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return 0
        return int(out) if out.isdigit() else 0

    if children() == 0:
        pytest.skip("wmic is not available to observe the child")
    process_stop({"handle": started.data["handle"]}, ctx)
    time.sleep(1)
    assert children() == 0


# -- a foreground command moves to the background (plan 0.4.0 #32) ------------------


def _shell_ctx(tmp_path: Path, registry: ProcessRegistry, call_id: str) -> ToolContext:
    from dataclasses import replace

    return replace(_ctx(tmp_path, registry), tool_call_id=call_id)


def test_the_owner_moves_a_running_command_to_the_background(tmp_path, registry) -> None:
    import threading

    from rinari.tools.native.shell import request_background, running_foreground, shell_exec

    ctx = _shell_ctx(tmp_path, registry, "call_long")
    body = "import time\nprint('instalando', flush=True)\ntime.sleep(60)\n"
    command = _script(tmp_path, "install.py", body)
    results: list = []
    thread = threading.Thread(
        target=lambda: results.append(shell_exec({"command": command, "timeout_s": 120}, ctx))
    )
    thread.start()
    deadline = time.monotonic() + 10
    while "call_long" not in running_foreground("s1") and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert request_background("s1", "call_long") is True
    thread.join(10)
    result = results[0]
    assert result.ok and result.data["background"] is True
    handle = registry.get(result.data["handle"])
    # Same process, same output so far, still running.
    assert handle.exit_code is None
    assert "instalando" in handle.stdout.text()
    assert request_background("s1", "call_long") is False


def test_a_long_command_moves_to_the_background_instead_of_dying(
    tmp_path, registry, monkeypatch
) -> None:
    from rinari.tools.native import shell

    monkeypatch.setattr(shell, "SHORT_EXPLICIT_TIMEOUT_S", 0.5)
    ctx = _shell_ctx(tmp_path, registry, "call_slow")
    result = shell.shell_exec(
        {"command": _script(tmp_path, "slow.py", SERVER), "timeout_s": 1}, ctx
    )
    assert result.ok and result.data["background"] is True and result.data["reason"] == "timeout"
    assert registry.get(result.data["handle"]).exit_code is None


def test_a_short_explicit_limit_or_an_interactive_command_still_stops(tmp_path, registry) -> None:
    from rinari.tools.native.shell import _waits_for_a_person, shell_exec

    ctx = _shell_ctx(tmp_path, registry, "call_quick")
    result = shell_exec({"command": _script(tmp_path, "slow.py", SERVER), "timeout_s": 1}, ctx)
    assert not result.ok and result.error.code.value == "TIMEOUT"
    assert registry.list() == []
    assert _waits_for_a_person("ssh server") and _waits_for_a_person("vim notes.txt")
    assert not _waits_for_a_person("ssh server uptime")
