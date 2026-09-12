import threading
from types import SimpleNamespace

import pytest

from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.processes import DesktopProcesses
from rinari.tools.native.process import ProcessRegistry


def service(registry):
    server = SimpleNamespace(
        _services=SimpleNamespace(sessions=SimpleNamespace(show=lambda ref: ref)),
        _turns=SimpleNamespace(_desktop_processes={"owner": registry}),
        _pty=SimpleNamespace(list=lambda: []),
        _previews=SimpleNamespace(_lock=threading.RLock(), _items={}),
    )
    return DesktopProcesses(server)


def test_lists_owned_process_and_rejects_another_session():
    registry = SimpleNamespace(
        list=lambda: [
            SimpleNamespace(
                id="proc_001",
                command=["python", "site with spaces.py"],
                cwd="C:/Site",
                started_at=1,
                process=SimpleNamespace(pid=999, poll=lambda: None),
            )
        ]
    )
    api = service(registry)
    row = api.list({"session_id": "owner"})["processes"][0]
    assert row["running"] and row["can_stop"]
    assert '"site with spaces.py"' in row["command"]
    assert api.list({"session_id": "other"})["processes"] == []
    with pytest.raises(EngineProtocolError):
        api.stop({"session_id": "other", "id": row["id"]})


def test_finished_process_stop_is_idempotent_and_output_is_bounded():
    handle = SimpleNamespace(
        id="proc_001",
        command="completed",
        cwd="C:/Site",
        started_at=1,
        process=SimpleNamespace(pid=999, poll=lambda: 0),
        stdout=SimpleNamespace(text=lambda: "ñ" * 70000, truncated=False),
        stderr=SimpleNamespace(text=lambda: "aviso", truncated=False),
    )
    api = service(SimpleNamespace(list=lambda: [handle]))
    params = {"session_id": "owner", "id": "process:proc_001"}
    output = api.read(params)
    assert len(output["stdout"]) == 64000 and output["truncated"]
    assert output["stderr"] == "aviso"
    assert not api.stop(params)["running"]
    assert not api.stop(params)["running"]


def test_background_output_arrives_before_process_exit(tmp_path):
    import sys
    import time

    registry = ProcessRegistry()
    identity = registry.start(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready', flush=True); time.sleep(2)",
        ],
        cwd=str(tmp_path),
    )
    handle = registry.get(identity)
    try:
        deadline = time.monotonic() + 1.5
        while not handle.stdout.text() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "ready" in handle.stdout.text()
        assert handle.process.poll() is None
        api = service(registry)
        assert not api.stop({"session_id": "owner", "id": f"process:{identity}"})["running"]
    finally:
        registry.wait(handle, 3)


def test_recent_logs_continue_after_model_capture_limit():
    from rinari.tools.native.process import _ProcessBuffer

    buffer = _ProcessBuffer(100)
    buffer.write(b"a" * 70000)
    buffer.write("Servidor actualizado: ñ".encode())
    assert buffer.text() == "a" * 100
    assert buffer.tail_text().endswith("Servidor actualizado: ñ")
    assert len(buffer._tail) <= 64000
    assert buffer.tail_truncated


def test_preview_stop_is_routed_to_its_owning_preview_service():
    api = service(SimpleNamespace(list=lambda: []))
    previews = api.server._previews
    calls = []
    previews._items["p1"] = SimpleNamespace(
        id="p1", session_id="owner", process_id=None, server=object(),
        command=None, root="C:/Site", url="http://127.0.0.1:8123/",
    )
    previews.stop = lambda params: calls.append(params)
    row = api.list({"session_id": "owner"})["processes"][0]
    assert row["kind"] == "preview" and row["can_stop"]
    assert not api.stop({"session_id": "owner", "id": row["id"]})["running"]
    assert calls == [{"session_id": "owner", "preview_id": "p1"}]
