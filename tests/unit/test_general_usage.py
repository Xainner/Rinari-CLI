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


def test_cache_reads_are_found_in_every_known_shape() -> None:
    """Only the standard field was read: llama.cpp reports `timings.cache_n` and
    DeepSeek's API `prompt_cache_hit_tokens` (plan 0.4.0 v2, V2-11)."""
    from rinari.providers.adapters.openai_compatible import _usage_from_openai

    standard = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}
    assert _usage_from_openai(standard).cached_input_tokens == 80
    deepseek = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 70}
    assert _usage_from_openai(deepseek).cached_input_tokens == 70
    llama = {"prompt_tokens": 100}
    assert _usage_from_openai(llama, {"cache_n": 60, "prompt_n": 40}).cached_input_tokens == 60
    assert _usage_from_openai({"prompt_tokens": 100}).cached_input_tokens is None
    zero = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    assert _usage_from_openai(zero, {"cache_n": 60}).cached_input_tokens == 0


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


def test_codex_requests_carry_the_conversation_session_header() -> None:
    """The subscription backend kept no cache without `session_id`: 0 % in 8
    calls, ~90 % with it (measured 2026-10-18, plan 0.4.0 v2, V2-10)."""
    import uuid

    adapter = CodexResponsesAdapter()
    request = ModelRequest(model="gpt-x", messages=(ChatMessage.user("hola"),), session_id="ses_1")
    first = adapter.request_headers("token", request)["session_id"]
    assert uuid.UUID(first)
    assert adapter.request_headers("token", request)["session_id"] == first
    other = ModelRequest(model="gpt-x", messages=(ChatMessage.user("hola"),), session_id="ses_2")
    assert adapter.request_headers("token", other)["session_id"] != first
    bare = ModelRequest(model="gpt-x", messages=(ChatMessage.user("hola"),))
    assert "session_id" not in adapter.request_headers("token", bare)


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


def test_anthropic_marks_tools_system_and_newest_message_for_caching() -> None:
    from rinari.models.types import ToolSchema
    from rinari.providers.adapters.anthropic import AnthropicAdapter

    blocks = [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": "a"},
    ]
    signed = ChatMessage(
        role="assistant", content="a", continuation={"protocol": "anthropic", "blocks": blocks}
    )
    request = ModelRequest(
        model="claude-x",
        messages=(ChatMessage.system("rules"), ChatMessage.user("hola"), signed),
        tools=(
            ToolSchema("fs.read", "read", {"type": "object"}),
            ToolSchema("fs.list", "list", {"type": "object"}),
        ),
    )
    payload = AnthropicAdapter()._payload(request, stream=True)
    assert payload["system"] == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in payload["tools"][0]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    last = payload["messages"][-1]["content"]
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in last[0]  # thinking blocks may not carry one
    assert "cache_control" not in blocks[-1], "the stored continuation must not change"


def test_anthropic_caching_can_be_turned_off_for_a_proxy() -> None:
    from rinari.providers.adapters.anthropic import AnthropicAdapter

    class Proxy(AnthropicAdapter):
        prompt_caching = False

    request = ModelRequest(
        model="claude-x", messages=(ChatMessage.system("r"), ChatMessage.user("h"))
    )
    assert Proxy()._payload(request, stream=True)["system"] == "r"


def test_openai_gets_a_prompt_cache_key_and_other_servers_do_not() -> None:
    from rinari.providers.adapters.http import openai_cache_key

    request = ModelRequest(model="m", messages=(ChatMessage.user("h"),), session_id="ses_1")
    assert openai_cache_key({}, "https://api.openai.com/v1/responses", request) == {
        "prompt_cache_key": "ses_1"
    }
    assert openai_cache_key({}, "https://opencode.ai/zen/go/v1/chat/completions", request) == {}
    assert openai_cache_key({}, "http://localhost:8080/v1/chat/completions", request) == {}


def test_nothing_settles_while_the_request_fits_then_one_jump_and_it_stays() -> None:
    # F45: settling every few rounds moved the middle of the request; a local
    # model re-read ~85k tokens at once. By pressure: nothing below SETTLE_AT,
    # then one jump down to SETTLE_TO, and the boundary stays put after it.
    from rinari.context.settle import SETTLE_AT, SETTLE_TO, SettleState, plan_boundary
    from rinari.context.tokens import estimate_tokens

    history = [ChatMessage.user("fix it")]
    for i in range(12):
        history += _round(i, 8000)
    window = 100_000
    state = SettleState()
    assert estimate_tokens(history=history) < SETTLE_AT * window
    assert plan_boundary(history, state, window_tokens=window) == 0
    assert settle_old_observations(history, boundary=0) == history

    for i in range(12, 30):
        history += _round(i, 8000)
    assert estimate_tokens(history=history) >= SETTLE_AT * window
    boundary = plan_boundary(history, state, window_tokens=window)
    assert boundary > 0
    shaped = settle_old_observations(history, boundary=boundary)
    assert estimate_tokens(history=shaped) <= SETTLE_TO * window + 2100  # one round of slack
    # The newest rounds are never settled.
    assert all(len(m.content) == 8000 for m in shaped[-16:] if m.role == "tool")

    # A few more rounds: below the trigger again, so the boundary (and the
    # cached prefix) does not move.
    history += _round(30, 8000)
    assert plan_boundary(history, state, window_tokens=window) == boundary
    assert settle_old_observations(history, boundary=boundary)[: len(shaped)] == shaped


def test_a_rewritten_history_starts_the_boundary_over_and_no_window_keeps_blocks() -> None:
    from rinari.context.settle import SettleState, plan_boundary, settled_boundary

    history = [m for i in range(30) for m in _round(i, 8000)]
    state = SettleState()
    assert plan_boundary(history, state, window_tokens=60_000) > 0
    compacted = [ChatMessage.user("summary"), *history[-4:]]
    assert state.current(compacted) == 0
    assert plan_boundary(history, SettleState(), window_tokens=None) == settled_boundary(history)
