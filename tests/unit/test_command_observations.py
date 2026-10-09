"""Command output reaches the model compact and bounded, never lossy.

Shell results used to travel as a JSON envelope (escaped newlines, the
command and cwd echoed back) and stayed inline up to 64 KiB because the
`context.artifact_output_threshold_kb` setting was never read.
"""

from __future__ import annotations

import json
import sys

from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import SystemClock
from rinari.tools.definition import (
    ArtifactRef,
    ToolContext,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.native.shell import shell_tools
from rinari.tools.observations import project_result
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import DEFAULT_OUTPUT_THRESHOLD_BYTES, ToolRuntime


def shell_result(**overrides):
    data = {
        "command": "pytest -q",
        "cwd": "/work/repo",
        "exit_code": 0,
        "stdout": 'line "one"\nline two\n',
        "stderr": "",
        "truncated": False,
        "timeout": {"requested_s": None, "effective_s": 60.0, "source": "default"},
    }
    data.update(overrides.pop("data", {}))
    return ToolResult(ok=overrides.pop("ok", True), data=data, **overrides)


def context(tmp_path):
    return ToolContext(
        session_id="s",
        kind="CHAT",
        cwd=tmp_path,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.FULL_ACCESS,
        sandbox=FilesystemSandbox(tmp_path, write_roots=(tmp_path,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=1_000_000),
        artifact_root=tmp_path / "artifacts",
        clock=SystemClock(),
    )


def runtime(**kwargs):
    registry = ToolRegistry()
    registry.register_all(shell_tools())
    return ToolRuntime(registry, PolicyEngine(), ApprovalEngine(prompt=lambda _: "y"), **kwargs)


def test_successful_command_is_plain_text_without_echoing_the_request():
    text = shell_result().to_model_text("shell.exec")
    assert text.startswith("exit_code: 0 (exited_zero; task not verified")
    assert '--- stdout ---\nline "one"\nline two' in text
    # No JSON escaping, no request echo, no empty stderr section.
    assert '\\"' not in text and "\\n" not in text
    assert "pytest -q" not in text and "/work/repo" not in text and "stderr" not in text


def test_failed_command_keeps_error_command_and_cwd_for_diagnosis():
    failed = shell_result(
        ok=False,
        data={"exit_code": -1, "stderr": "boom\n"},
        error=ToolErrorInfo(ToolErrorCode.TIMEOUT, "Command exceeded timeout_s=5s", True),
    )
    text = failed.to_model_text("shell.exec")
    assert text.splitlines()[0] == "error: TIMEOUT (retryable): Command exceeded timeout_s=5s"
    assert "exit_code: -1 (failed" in text
    assert "command: pytest -q" in text and "cwd: /work/repo" in text
    assert "--- stderr ---\nboom" in text
    nonzero = shell_result(data={"exit_code": 2}).to_model_text("shell.exec")
    assert "exit_code: 2 (failed" in nonzero and "command: pytest -q" in nonzero


def test_other_tools_keep_the_json_envelope():
    result = ToolResult(ok=True, data={"stdout": "x", "exit_code": 0})
    assert json.loads(result.to_model_text("process.output"))["data"]["stdout"] == "x"
    background = ToolResult(ok=True, data={"handle": "p1", "running": True})
    assert json.loads(background.to_model_text("shell.exec"))["data"]["handle"] == "p1"


def test_large_output_keeps_head_and_tail_and_points_at_the_complete_artifact():
    saved = {}

    def spill(suffix, text):
        uri = f"artifact://s/runtime/{suffix}.txt"
        saved[uri] = text
        return ArtifactRef(uri=uri, name=suffix, kind="tool-output")

    lines = "".join(f"line {i}\n" for i in range(5000))
    source = shell_result(data={"exit_code": 1, "stdout": lines, "stderr": "FAILED test_x\n"})
    output = project_result(source, tool="shell.exec", budget=2000, spill=spill)
    text = output.to_model_text("shell.exec")
    assert len(text.encode("utf-8")) <= 2000
    assert output.truncated and output.data["delivery_partial"]
    stdout = output.data["stdout"]
    assert stdout.startswith("line 0\n") and stdout.endswith("line 4999\n")
    assert "bytes omitted" in stdout
    # The error stream and the recovery pointer survive the cut.
    assert "FAILED test_x" in text
    uri = output.data["recovery"]["uri"]
    assert f'artifact.read {{"uri": "{uri}"}}' in text
    # The artifact holds the complete observation in the same readable form.
    assert lines.rstrip("\n") in saved[uri] and "exit_code: 1" in saved[uri]


def test_a_single_huge_line_is_cut_instead_of_dropped():
    def spill(suffix, text):
        return ArtifactRef(uri=f"artifact://s/runtime/{suffix}", name=suffix)

    source = shell_result(data={"stdout": "x" * 200_000})
    output = project_result(source, tool="shell.exec", budget=8000, spill=spill)
    assert output.data["stdout"].startswith("x" * 1000)
    assert len(output.to_model_text("shell.exec").encode("utf-8")) <= 8000


def test_command_output_spills_above_the_configured_threshold(tmp_path):
    argv = [sys.executable, "-c", "[print('row', i) for i in range(3000)]"]
    small = runtime(spill_threshold_bytes=4 * 1024).execute(
        "shell.exec", {"argv": argv}, context(tmp_path), tool_call_id="big"
    )
    text = small.to_model_text("shell.exec")
    assert len(text.encode("utf-8")) <= 4 * 1024 + 1024
    assert small.data["delivery_partial"] and "row 2999" in small.data["stdout"]
    # The activity card still receives the complete structured result.
    assert small.full_observation.data["stdout"].count("row") == 3000
    assert small.presentation["exit_code"] == 0

    roomy = runtime(spill_threshold_bytes=1024 * 1024).execute(
        "shell.exec", {"argv": argv}, context(tmp_path), tool_call_id="whole"
    )
    assert "delivery_partial" not in roomy.data


def test_round_projection_applies_the_threshold_to_final_observations(tmp_path):
    from types import SimpleNamespace

    argv = [sys.executable, "-c", "[print('row', i) for i in range(3000)]"]
    rt = runtime(spill_threshold_bytes=4 * 1024)
    ctx = context(tmp_path)
    result = rt.execute("shell.exec", {"argv": argv}, ctx, tool_call_id="c1")
    call = SimpleNamespace(id="c1", name="shell.exec")
    (final,) = rt.project_round([(call, result)], ctx)
    assert len(final.to_model_text("shell.exec").encode("utf-8")) <= 5 * 1024
    assert "row 0" in final.data["stdout"] and "row 2999" in final.data["stdout"]


def test_default_threshold_is_sixteen_kib_and_matches_the_config_default():
    from rinari.application.config.loader import load_defaults
    from rinari.application.config.schema import ContextSettings

    assert DEFAULT_OUTPUT_THRESHOLD_BYTES == 16 * 1024
    assert ContextSettings().artifact_output_threshold_kb * 1024 == DEFAULT_OUTPUT_THRESHOLD_BYTES
    assert load_defaults()["context"]["artifact_output_threshold_kb"] == 16


def test_session_runtime_reads_the_configured_threshold(tmp_path, monkeypatch):
    from rinari.application.context import build_app_context
    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.cli import agent_runtime
    from rinari.shared.clock import FakeClock
    from rinari.shared.paths import ENV_HOME

    home = tmp_path / "rinari-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[context]\nartifact_output_threshold_kb = 8\n", encoding="utf-8"
    )
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.setenv("RINARI_KEYRING", "0")
    app = build_app_context(home=str(home), clock=FakeClock(start=1_700_000_000.0, step=1.0))
    try:
        user_home = tmp_path / "home"
        user_home.mkdir()
        services = build_services(app, user_home=user_home)
        services.providers.add(
            AddProviderInput(
                alias="fake",
                provider_type="openai",
                endpoint="http://127.0.0.1:9/v1",
                secret="dummy-secret-not-real",
            )
        )
        services.models.add("fake", "fake-model-1", "fake-one")
        services.providers.use("fake")
        work = tmp_path / "work"
        work.mkdir()
        record = services.sessions.start(work, forced_chat=True).session
        monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_: object())
        session = agent_runtime.build_agent_session(
            services, record, interactive=False, user_home=user_home
        )
        assert session.loop._tools.spill_threshold_bytes == 8 * 1024
    finally:
        app.close()
