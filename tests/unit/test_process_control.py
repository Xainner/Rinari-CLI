"""Process control + live streaming tests (real subprocesses, no network)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from rinari.policy.engine import CAPABILITY_PROCESS_LOCAL, PolicyAction, PolicyEngine, SessionScope
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.process import ProcessRegistry
from rinari.tools.native.shell import shell_exec


def _ctx(tmp_path: Path, root: Path, *, sink=None, processes=None) -> ToolContext:
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
        cancellation=None,
        output_sink=sink,
        processes=processes,
    )


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    return tmp_path, root


def _python(code: str) -> str:
    return f"python -c {code!r}" if sys.platform != "win32" else f'python -c "{code}"'


def test_process_start_wait_output(env) -> None:
    tmp_path, root = env
    ctx = _ctx(tmp_path, root, processes=ProcessRegistry())
    registry = ctx.processes
    handle_id = registry.start(_python("print('hello-proc')"))
    assert handle_id.startswith("proc_")

    from rinari.tools.native.process import process_output, process_wait

    wait = process_wait({"handle": handle_id, "timeout_s": 10}, ctx)
    assert wait.ok is True
    assert wait.data["exit_code"] == 0
    assert wait.data["timed_out"] is False

    out = process_output({"handle": handle_id}, ctx)
    assert out.ok is True
    assert "hello-proc" in out.data["stdout"].replace("\r", "")
    assert out.data["running"] is False


def test_process_output_while_running(env) -> None:
    from rinari.tools.native.process import process_output, process_wait

    tmp_path, root = env
    ctx = _ctx(tmp_path, root, processes=ProcessRegistry())
    code = "import time; print('step1',flush=True); time.sleep(1.5); print('step2',flush=True)"
    handle_id = ctx.processes.start(_python(code))
    deadline = time.time() + 5
    first = None
    while time.time() < deadline:
        first = process_output({"handle": handle_id}, ctx).data
        if "step1" in first["stdout"]:
            break
        time.sleep(0.1)
    assert "step1" in first["stdout"]
    assert first["running"] is True

    wait = process_wait({"handle": handle_id, "timeout_s": 10}, ctx)
    assert wait.data["exit_code"] == 0
    final = process_output({"handle": handle_id}, ctx).data
    assert "step2" in final["stdout"]


def test_process_signal_kill(env) -> None:
    from rinari.tools.native.process import process_list, process_signal, process_wait

    tmp_path, root = env
    ctx = _ctx(tmp_path, root, processes=ProcessRegistry())
    if sys.platform == "win32":
        sleeper = "ping -n 30 127.0.0.1 >nul"
    else:
        sleeper = "sleep 30"
    handle_id = ctx.processes.start(sleeper)

    sig = process_signal({"handle": handle_id, "signal": "KILL"}, ctx)
    assert sig.ok is True

    wait = process_wait({"handle": handle_id, "timeout_s": 10}, ctx)
    assert wait.data["exit_code"] is not None
    listing = process_list({}, ctx)
    assert listing.ok is True
    entry = next(p for p in listing.data["processes"] if p["handle"] == handle_id)
    assert entry["running"] is False


def test_process_unknown_handle(env) -> None:
    from rinari.tools.native.process import process_output, process_wait

    tmp_path, root = env
    ctx = _ctx(tmp_path, root, processes=ProcessRegistry())
    assert process_wait({"handle": "proc_999"}, ctx).ok is False
    assert process_output({"handle": "proc_999"}, ctx).ok is False


def test_process_local_is_allowed_by_policy(env) -> None:
    tmp_path, root = env
    scope = SessionScope(
        kind="PROJECT", root=root, cwd=root, profile="workspace", user_home=tmp_path
    )
    decision = PolicyEngine().decide(CAPABILITY_PROCESS_LOCAL, scope, risk="low", risk_class="none")
    assert decision.action is PolicyAction.ALLOW


def test_session_interruption_state() -> None:
    from types import SimpleNamespace

    from rinari.cli import agent_runtime

    calls: list[str] = []
    services = SimpleNamespace(
        ctx=SimpleNamespace(
            clock=FakeClock(start=1_700_000_000.0, step=0.01),
            session_repo=SimpleNamespace(update=lambda r: calls.append(r.state)),
        )
    )
    record = SimpleNamespace(state="active", last_active_at="", updated_at="")
    agent_runtime._set_session_state(services, record, "interrupted")
    assert record.state == "interrupted"
    assert calls == ["interrupted"]
    agent_runtime._set_session_state(services, record, "interrupted")  # no-op
    assert calls == ["interrupted"]
    agent_runtime._set_session_state(services, record, agent_runtime.STATE_ACTIVE)
    assert calls == ["interrupted", "active"]


def test_shell_live_streaming(env) -> None:
    tmp_path, root = env
    received: list[tuple[str, str]] = []
    ctx = _ctx(tmp_path, root, sink=lambda s, c: received.append((s, c)))
    code = "import time; print('a',flush=True); time.sleep(0.3); print('b',flush=True)"
    result = shell_exec({"command": _python(code), "timeout_s": 15}, ctx)
    assert result.ok is True
    assert "a" in result.data["stdout"].replace("\r", "")
    assert "b" in result.data["stdout"].replace("\r", "")
    streamed = "".join(chunk for stream, chunk in received if stream == "stdout")
    assert "a" in streamed and "b" in streamed
