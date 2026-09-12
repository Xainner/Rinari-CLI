"""Real process acceptance checks for readable command activity."""

import sys

import pytest

from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.shell import shell_exec


def context(tmp_path, sink=None):
    return ToolContext(
        session_id="ses_review",
        kind="PROJECT",
        cwd=tmp_path,
        project_root=tmp_path,
        user_home=tmp_path,
        profile="workspace",
        sandbox=FilesystemSandbox(read_root=tmp_path, write_roots=(tmp_path,)),
        limits=ProcessLimits(timeout_s=10, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        output_sink=sink,
    )


@pytest.mark.parametrize("redacted", [False, True])
def test_first_output_arrives_while_process_is_still_running(tmp_path, redacted):
    gate = tmp_path / "release"
    received = []

    def sink(stream, text):
        received.append((stream, text))
        if stream == "stdout" and "ready" in text:
            gate.write_text("release", encoding="utf-8")

    code = (
        "import pathlib,sys,time; print('ready',flush=True); "
        "p=pathlib.Path(sys.argv[1]);\n"
        "while not p.exists(): time.sleep(.02)\n"
        "print('finished',flush=True)"
    )
    arguments = {"argv": [sys.executable, "-u", "-c", code, str(gate)], "timeout_s": 3}
    if redacted:
        result = runtime_with_secrets(("synthetic-secret-value",)).execute(
            "shell.exec", arguments, context(tmp_path, sink), tool_call_id="live"
        )
    else:
        result = shell_exec(arguments, context(tmp_path, sink))
    assert result.ok, result.error
    assert gate.exists()
    from rinari.runtime.agent import _tool_activity_presentation

    assert "finished" in _tool_activity_presentation("shell.exec", arguments, result)["stdout"]


def test_argv_preserves_unicode_quotes_and_dollar_with_separate_stderr(tmp_path):
    literal = 'Mañana "café" $HOME \\ ruta con espacios'
    code = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8'); "
        "sys.stderr.reconfigure(encoding='utf-8'); print(sys.argv[1]); "
        "print('Aviso: información',file=sys.stderr)"
    )
    result = shell_exec({"argv": [sys.executable, "-c", code, literal]}, context(tmp_path))
    assert result.ok
    assert result.data["exit_code"] == 0
    assert result.data["stdout"].strip() == literal
    assert result.data["stderr"].strip() == "Aviso: información"


def test_generic_tool_presentation_keeps_structured_data():
    from rinari.engine_protocol.turns import _safe_presentation

    value = _safe_presentation({"kind": "tool", "data": {"files": ["mañana.txt"], "count": 1}})
    assert isinstance(value["data"], dict)
    assert value["data"]["files"] == ["mañana.txt"]


def test_visible_output_truncation_is_explicit():
    from rinari.engine_protocol.turns import _safe_presentation

    value = _safe_presentation({"kind": "command", "stdout": "x" * 70_000, "stderr": "Aviso"})
    assert len(value["stdout"]) <= 64_000
    assert value["stderr"] == "Aviso"
    assert value["truncated"] is True


def runtime_with_secrets(secrets=()):
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PolicyEngine
    from rinari.shared.redaction import Redactor
    from rinari.tools.native.shell import shell_tools
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    registry.register_all(shell_tools())
    return ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda _: "y"),
        redactor=Redactor(secrets),
        spill_threshold_bytes=512,
    )


def test_long_command_preserves_failure_and_streams_after_model_spill(tmp_path):
    from rinari.runtime.agent import _tool_activity_presentation

    arguments = {
        "argv": [
            sys.executable,
            "-c",
            "import sys; print('x'*3000); print('failure',file=sys.stderr); sys.exit(7)",
        ]
    }
    result = runtime_with_secrets().execute(
        "shell.exec", arguments, context(tmp_path), tool_call_id="long"
    )
    presentation = _tool_activity_presentation("shell.exec", arguments, result)
    assert presentation["exit_code"] == 7
    assert presentation["status"] == "failed"
    assert presentation["stdout"]
    assert "failure" in presentation["stderr"]
    assert presentation["artifacts"]


def test_known_secret_is_redacted_when_split_across_live_chunks(tmp_path):
    secret = "synthetic-secret-value"
    output = []
    script = (
        "import sys,time; sys.stdout.write('synthetic-'); sys.stdout.flush(); "
        "time.sleep(.1); sys.stdout.write('secret-value\\n'); sys.stdout.flush()"
    )
    result = runtime_with_secrets((secret,)).execute(
        "shell.exec",
        {"argv": [sys.executable, "-u", "-c", script]},
        context(tmp_path, lambda stream, text: output.append(text)),
        tool_call_id="redaction",
    )
    assert result.ok
    assert secret not in "".join(output)
    assert "[REDACTED]" in "".join(output)


def test_capture_artifact_retains_output_beyond_visible_limit(tmp_path):
    arguments = {"argv": [sys.executable, "-c", "print('x'*100000); print('END_OF_CAPTURE')"]}
    result = runtime_with_secrets().execute(
        "shell.exec", arguments, context(tmp_path), tool_call_id="capture"
    )
    capture = tmp_path / "artifacts" / "ses_review" / "runtime" / "capture-stdout.txt"
    assert capture.exists()
    assert capture.read_text(encoding="utf-8").endswith("END_OF_CAPTURE\n")
    assert result.captured_output is None
    assert any(ref.uri.endswith("capture-stdout.txt") for ref in result.artifacts)


def test_execution_capture_shares_limit_between_streams(monkeypatch):
    import rinari.tools.native.shell as shell

    monkeypatch.setattr(shell, "MAX_CAPTURE_BYTES", 10)
    capture = shell._ExecutionCapture()
    capture.write("stdout", b"123456")
    capture.write("stderr", b"abcdef")
    value = capture.result()
    assert value["stdout"] == "123456"
    assert value["stderr"] == "abcd"
    assert value["truncated"] is True


def test_desktop_protocol_reads_runtime_capture_without_database_metadata(app_ctx):
    from types import SimpleNamespace

    from rinari.artifacts.store import ArtifactStore, ArtifactURIError
    from rinari.engine_protocol.server import EngineServer

    store = ArtifactStore(app_ctx)
    target = store._root() / "ses_review" / "runtime" / "capture-stdout.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes("Mañana\r\nCafé\r\n".encode())
    host = EngineServer.__new__(EngineServer)
    host._services = SimpleNamespace(artifacts=store)
    result = host._artifact_read({"uri": "artifact://ses_review/runtime/capture-stdout.txt"})
    assert result["text"] == "Mañana\r\nCafé\r\n"
    assert result["truncated"] is False
    assert result["artifact"]["byte_count"] == target.stat().st_size
    with pytest.raises(ArtifactURIError):
        host._artifact_read({"uri": "artifact://ses_review/../capture-stdout.txt"})


def test_live_stream_stops_at_capture_limit_but_process_finishes(tmp_path, monkeypatch):
    import rinari.tools.native.shell as shell
    from rinari.runtime.agent import _tool_activity_presentation

    monkeypatch.setattr(shell, "MAX_CAPTURE_BYTES", 70_000)
    output = []
    result = runtime_with_secrets().execute(
        "shell.exec",
        {"argv": [sys.executable, "-c", "print('x'*120000)"]},
        context(tmp_path, lambda stream, text: output.append(text)),
        tool_call_id="capped",
    )
    assert result.ok
    assert len("".join(output).encode()) <= 70_000
    assert _tool_activity_presentation("shell.exec", {}, result)["capture_truncated"] is True
