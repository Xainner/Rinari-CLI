"""Lote 1 from real usage (2026-10-08): improvements every user gets."""

from __future__ import annotations

import sys

import pytest

from rinari.context.settle import settle_old_observations
from rinari.context.windows import normalize
from rinari.models.types import ChatMessage, ModelRequest, ToolCall
from rinari.providers.adapters.responses import _usage_from_responses
from rinari.providers.adapters.subscriptions import CodexResponsesAdapter
from rinari.runtime.agent import _foreign_script
from rinari.tools.native.shell import shell_argv
from rinari.verify.gate import evaluate_gate


def _round(i: int, size: int) -> list[ChatMessage]:
    call = ToolCall(id=f"c{i}", name="fs.read", arguments={"path": f"f{i}.py"})
    return [
        ChatMessage.assistant("", (call,)),
        ChatMessage.tool_result(f"c{i}", "fs.read", "x" * size),
    ]


def test_old_tool_results_settle_in_blocks_and_history_is_untouched() -> None:
    history = [ChatMessage.user("fix it")]
    for i in range(20):
        history += _round(i, 5000 if i != 3 else 100)
    shaped = settle_old_observations(history)
    results = [m for m in shaped if m.role == "tool"]
    # 20 rounds: the newest 8 stay; 12 older ones settle as one block of 8.
    settled = [m for m in results if m.content.startswith("[Earlier result of fs.read")]
    assert len(settled) == 7  # rounds 0-7 minus the short one (round 3)
    assert results[3].content == "x" * 100
    assert all(len(m.content) == 5000 for m in results[8:])
    assert all(m.tool_call_id for m in shaped if m.role == "tool")
    assert history[2].content == "x" * 5000, "the persisted history must not change"
    # Adding rounds within the same block does not move the boundary: the
    # request prefix stays the same for the provider's cache.
    more = history + _round(20, 5000) + _round(21, 5000)
    assert settle_old_observations(more)[: len(shaped)] == shaped


def test_a_settled_result_keeps_its_artifact_reference() -> None:
    history = []
    for i in range(16):
        round_ = _round(i, 10)
        round_[1] = ChatMessage.tool_result(
            f"c{i}", "shell.exec", "y" * 3000 + " full output in artifact://ses/runtime/out.txt"
        )
        history += round_
    note = settle_old_observations(history)[1].content
    assert "artifact://ses/runtime/out.txt" in note and "artifact.read" in note


def test_few_rounds_are_never_settled() -> None:
    history = [m for i in range(9) for m in _round(i, 9000)]
    assert settle_old_observations(history) == history


def test_responses_usage_reads_cache_and_reasoning() -> None:
    usage = _usage_from_responses(
        {
            "input_tokens": 120_000,
            "output_tokens": 900,
            "input_tokens_details": {"cached_tokens": 110_000},
            "output_tokens_details": {"reasoning_tokens": 300},
        }
    )
    assert (usage.cached_input_tokens, usage.reasoning_tokens) == (110_000, 300)
    assert _usage_from_responses({"input_tokens": 5}).cached_input_tokens is None


def test_codex_requests_share_a_prompt_cache_per_conversation() -> None:
    request = ModelRequest(
        model="gpt-x",
        messages=(ChatMessage.system("sys"), ChatMessage.user("hola")),
        session_id="ses_1",
    )
    payload = CodexResponsesAdapter()._responses_payload(request, stream=True)
    assert payload["prompt_cache_key"] == "ses_1"
    bare = ModelRequest(model="gpt-x", messages=(ChatMessage.user("hola"),))
    assert "prompt_cache_key" not in CodexResponsesAdapter()._responses_payload(bare, stream=True)


def test_llama_cpp_reports_its_window_inside_meta() -> None:
    assert normalize({"id": "q", "meta": {"n_ctx_train": 131072}}) == {"max_context_tokens": 131072}
    assert normalize({"meta": {"n_ctx": 32768, "n_ctx_train": 131072}}) == {
        "max_context_tokens": 32768
    }
    assert normalize({"context_length": 8192, "meta": {"n_ctx": 4}}) == {"max_context_tokens": 8192}


def test_shell_choice_wraps_the_command_only_at_launch(monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: f"/bin/{name}" if name != "pwsh" else None
    )
    assert shell_argv("ls | head -3", None) == "ls | head -3"
    assert shell_argv("ls | head -3", "default") == "ls | head -3"
    assert shell_argv(["git", "status"], "bash") == ["git", "status"]
    assert shell_argv("Get-Date", "powershell") == [
        "/bin/powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-Date",
    ]
    with pytest.raises(ValueError, match="shell must be one of"):
        shell_argv("x", "zsh")
    if sys.platform != "win32":
        with pytest.raises(ValueError, match="only available on Windows"):
            shell_argv("dir", "cmd")


def test_shell_exec_reports_a_missing_shell_without_running(monkeypatch, tmp_path) -> None:
    import shutil

    from rinari.tools.native.shell import shell_exec

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    result = shell_exec({"command": "Get-Date", "shell": "powershell"}, object())
    assert not result.ok and "PowerShell is not installed" in result.error.message


def test_a_reply_in_another_script_is_caught() -> None:
    history = [ChatMessage.user("¿Puedes revisar el informe del servidor?")]
    chinese = "服务器报告显示内存使用率很高、建议重启服务并检查日志文件以找出问题原因。" * 2
    assert _foreign_script(history, chinese)
    assert not _foreign_script(history, "Claro, el informe muestra memoria alta en el servidor.")
    # A user who writes in Chinese gets Chinese.
    assert not _foreign_script([ChatMessage.user("请检查服务器报告")], chinese)
    # A few names in another script inside a Spanish reply are fine.
    assert not _foreign_script(history, "El archivo 日本 se llama así; " + "texto " * 40)


def test_work_that_is_not_code_accepts_any_check() -> None:
    passed = [{"id": "v1", "kind": "manual", "result": "passed", "summary": "ok", "detail": ""}]
    assert evaluate_gate(records=passed, required_kinds=()).outcome == "DONE"
    assert evaluate_gate(records=[], required_kinds=()).outcome == "IMPLEMENTED_UNVERIFIED"
    failed = [{"id": "v2", "kind": "manual", "result": "failed", "summary": "no", "detail": ""}]
    assert evaluate_gate(records=failed, required_kinds=()).outcome == "FAILED"
    assert evaluate_gate(records=passed, required_kinds=("test",)).outcome == "PARTIAL"


def test_the_prompt_knows_today_where_it_runs_and_which_model_answers() -> None:
    """A model answered "today is July 9" and named its base model."""
    from types import SimpleNamespace

    from rinari.cli.agent_runtime import _runtime_facts
    from rinari.shared.clock import FakeClock

    clock = FakeClock(start=1_791_500_000.0, step=0.0)  # 2026-10-08
    provider = SimpleNamespace(alias="local-llama")
    model = SimpleNamespace(alias="qwen-27b", provider_model_id="qwen3.8-27b")
    services = SimpleNamespace(
        ctx=SimpleNamespace(clock=clock),
        providers=SimpleNamespace(get=lambda _id: provider),
        models=SimpleNamespace(resolve=lambda _m, _p: model),
    )
    record = SimpleNamespace(provider_id="prov_1", model_id="mdl_1")
    facts = _runtime_facts(services, record)
    assert facts["today"].startswith("2026-10-0")
    assert "UTC" in facts["timezone"] and facts["os"]
    assert "shell.exec" in facts["shell"]
    assert facts["model"].startswith("qwen-27b (qwen3.8-27b) via local-llama")
    # A model that cannot be resolved leaves the rest of the facts in place.
    services.models = SimpleNamespace(resolve=lambda *_a: (_ for _ in ()).throw(KeyError()))
    assert "model" not in _runtime_facts(services, record)
