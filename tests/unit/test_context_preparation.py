from types import SimpleNamespace

import pytest

from rinari.artifacts.store import ArtifactStore
from rinari.context.preparation import prepare, request_size
from rinari.context.projection import ContextPreparationError, project, render
from rinari.context.service import ContextService
from rinari.models.types import ChatMessage, ModelRequest, ModelResponse, ProviderCapabilities
from rinari.prompts.assembler import AssemblerContext
from rinari.runtime.agent import AgentContext
from rinari.runtime.cancellation import CancellationToken
from rinari.storage.records import SessionRecord


def setup(app_ctx):
    now = "2026-09-13T00:00:00Z"
    app_ctx.session_repo.insert(
        SessionRecord(
            id="compact",
            kind="CHAT",
            title="test",
            project_id=None,
            project_root_snapshot=None,
            created_cwd="/tmp",
            current_cwd="/tmp",
            provider_id="",
            model_id="fake",
            profile_id="",
            mode="default",
            state="active",
            compact_state=None,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )
    history = [ChatMessage.user("old decision " + "x" * 500), ChatMessage.assistant("yes")] * 30
    # Real messages have distinct durable identities.
    history = [ChatMessage(role=m.role, content=m.content) for m in history]
    history.append(ChatMessage.user("continue"))
    ctx = AgentContext(
        session_id="compact",
        model_ref="fake",
        tool_ctx=None,
        assembler_base=AssemblerContext(),
        history=history,
    )
    service = ContextService(app_ctx, ArtifactStore(app_ctx))
    calls = []

    def invoke(request):
        calls.append(request)
        assert not request.tools
        return ModelResponse(content="Goal: continue. Earlier decision: keep the existing design.")

    caller = SimpleNamespace(
        capabilities=lambda: ProviderCapabilities(max_context_tokens=3000), invoke=invoke
    )

    def rebuild(context):
        return ModelRequest(
            model="fake",
            messages=(ChatMessage.system(context.compact_state_text or "rules"), *context.history),
        )

    return service, ctx, caller, calls, rebuild


def test_preflight_persists_projection_without_mutating_originals(app_ctx):
    service, ctx, caller, calls, rebuild = setup(app_ctx)
    originals = list(ctx.history)
    events = []
    result = prepare(
        service,
        ctx,
        rebuild(ctx),
        caller,
        rebuild,
        lambda _, payload: events.append(payload),
        CancellationToken(),
    )
    state = app_ctx.session_repo.get(ctx.session_id).compact_state
    assert request_size(result) <= 1800
    assert project(originals, state) == ctx.history
    assert len(originals) > len(ctx.history)
    assert render(state) == ctx.compact_state_text
    assert [e["status"] for e in events] == ["started", "completed"]
    assert events[0]["compaction_id"] == events[-1]["compaction_id"]
    before = len(calls)
    prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    assert len(calls) == before


def test_failed_summary_leaves_history_and_state_unchanged(app_ctx):
    service, ctx, caller, _calls, rebuild = setup(app_ctx)
    original = list(ctx.history)
    caller.invoke = lambda request: ModelResponse(content="")
    with pytest.raises(ContextPreparationError):
        prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    assert ctx.history == original
    assert not app_ctx.session_repo.get(ctx.session_id).compact_state


def test_legacy_summary_does_not_invent_a_cut():
    history = [ChatMessage.user("one"), ChatMessage.assistant("two")]
    assert project(history, {"goal": "old summary"}) == history


def test_latest_owner_message_cannot_be_silently_discarded(app_ctx):
    service, ctx, caller, calls, rebuild = setup(app_ctx)
    ctx.history.append(ChatMessage.user("required " * 4000))
    with pytest.raises(ContextPreparationError):
        prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    assert not calls


def test_database_reload_uses_exact_persisted_projection(app_ctx):
    from rinari.application.services import build_services
    from rinari.cli.agent_runtime import _persist_new_messages, _restore_history

    service, ctx, caller, _calls, rebuild = setup(app_ctx)
    services = build_services(app_ctx)
    record = app_ctx.session_repo.get(ctx.session_id)
    originals = list(ctx.history)
    _persist_new_messages(services, record, originals, "original-turn")
    prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    fresh = app_ctx.session_repo.get(ctx.session_id)
    restored = _restore_history(services, fresh)
    assert [m.message_id for m in restored] == [m.message_id for m in ctx.history]
    assert len(restored) < len(originals)


def test_context_settings_preserve_manual_windows_and_existing_config(app_ctx):
    from rinari.context.settings import load, save

    service = SimpleNamespace(
        ctx=app_ctx, models=SimpleNamespace(resolve=lambda ref: SimpleNamespace(id=ref))
    )
    result = save(
        service,
        {
            "enabled": True,
            "compact_at_percent": 72,
            "model_id": None,
            "model_windows": {"configured": 64000},
        },
    )
    assert result == load(app_ctx)
    assert result["compact_at_percent"] == 72
    assert result["model_windows"] == {"configured": 64000}


def test_cancellation_does_not_publish_summary(app_ctx):
    from rinari.shared.errors import CancelledError

    service, ctx, caller, _calls, rebuild = setup(app_ctx)
    token = CancellationToken()
    original = list(ctx.history)

    def cancelled(request):
        token.cancel()
        return ModelResponse(content="summary")

    caller.invoke = cancelled
    with pytest.raises(CancelledError):
        prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, token)
    assert ctx.history == original
    assert not app_ctx.session_repo.get(ctx.session_id).compact_state


def test_normalization_separates_input_and_output_contracts():
    from rinari.context.windows import normalize

    assert normalize(
        {"context_length": 64000, "max_input_tokens": 32000, "max_output_tokens": 8192}
    ) == {"max_context_tokens": 64000, "max_input_tokens": 32000, "max_output_tokens": 8192}
    assert normalize({"context_length": True, "max_output_tokens": 4096}) == {
        "max_output_tokens": 4096
    }


def test_loop_compacts_before_first_conversation_request(app_ctx):
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PolicyEngine
    from rinari.prompts.assembler import PromptAssembler
    from rinari.runtime.agent import AgentLoop
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    service, ctx, caller, calls, _rebuild = setup(app_ctx)
    loop = AgentLoop(
        caller,
        ToolRuntime(ToolRegistry(), PolicyEngine(), ApprovalEngine()),
        PromptAssembler(),
        prepare_context=service.prepare,
    )
    result = loop.turn(ctx, "continue the work")
    assert result.kind == "answer"
    assert len(calls) >= 2
    assert calls[0].messages[0].content.startswith("Summarize conversation evidence")
    assert not calls[-1].messages[0].content.startswith("Summarize conversation evidence")
    assert request_size(calls[-1]) <= 1800


def test_successive_compaction_includes_previous_summary(app_ctx):
    service, ctx, caller, calls, rebuild = setup(app_ctx)
    prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    previous = app_ctx.session_repo.get(ctx.session_id).compact_state
    ctx.history.extend(ChatMessage.user("new evidence " + "z" * 500) for _ in range(40))
    ctx.history.append(ChatMessage.user("continue"))
    before = len(calls)
    prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    state = app_ctx.session_repo.get(ctx.session_id).compact_state
    assert previous["summary"] in calls[before].messages[-1].content
    assert set(previous["covered_message_ids"]) < set(state["covered_message_ids"])
    assert state["revision"] == 2


def test_persistence_failure_does_not_change_running_projection(app_ctx, monkeypatch):
    service, ctx, caller, _calls, rebuild = setup(app_ctx)
    history = list(ctx.history)

    def fail(_record):
        raise OSError("disk unavailable")

    monkeypatch.setattr(app_ctx.session_repo, "update", fail)
    with pytest.raises(OSError):
        prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    assert ctx.history == history
    assert ctx.compact_state_text is None


def test_explicit_overflow_retries_only_after_reducing_context(app_ctx):
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PolicyEngine
    from rinari.prompts.assembler import PromptAssembler
    from rinari.providers.errors import ProviderError, ProviderErrorCode
    from rinari.runtime.agent import AgentLoop
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    service, ctx, caller, _calls, _rebuild = setup(app_ctx)
    caller.capabilities = lambda: ProviderCapabilities(max_context_tokens=50000)
    requests = []

    def invoke(request):
        requests.append(request)
        if len(requests) == 1:
            raise ProviderError("Too large", code=ProviderErrorCode.CONTEXT_OVERFLOW)
        return ModelResponse(content="Keep the original decision.")

    caller.invoke = invoke
    loop = AgentLoop(
        caller,
        ToolRuntime(ToolRegistry(), PolicyEngine(), ApprovalEngine()),
        PromptAssembler(),
        prepare_context=service.prepare,
    )
    assert loop.turn(ctx, "continue").kind == "answer"
    assert len(requests) == 3
    assert request_size(requests[-1]) < request_size(requests[0])
    assert not ctx.force_compaction


def test_timeout_does_not_start_compaction_recovery(app_ctx):
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PolicyEngine
    from rinari.prompts.assembler import PromptAssembler
    from rinari.providers.errors import ProviderError, ProviderErrorCode
    from rinari.runtime.agent import AgentLoop
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    service, ctx, caller, _calls, _rebuild = setup(app_ctx)
    caller.capabilities = lambda: ProviderCapabilities(max_context_tokens=50000)
    requests = []

    def invoke(request):
        requests.append(request)
        raise ProviderError("Timed out", code=ProviderErrorCode.TIMEOUT)

    caller.invoke = invoke
    loop = AgentLoop(
        caller,
        ToolRuntime(ToolRegistry(), PolicyEngine(), ApprovalEngine()),
        PromptAssembler(),
        prepare_context=service.prepare,
    )
    with pytest.raises(ProviderError):
        loop.turn(ctx, "continue")
    assert len(requests) == 1
    assert not app_ctx.session_repo.get(ctx.session_id).compact_state


def test_summary_evidence_does_not_treat_approval_requests_as_grants(app_ctx):
    service, ctx, *_ = setup(app_ctx)
    service._persist_event(
        ctx.session_id, "PolicyDecision", {"action": "ask", "capability": "fs.write"}
    )
    service._persist_event(ctx.session_id, "ApprovalDenied", {"capability": "fs.write"})
    service._persist_event(ctx.session_id, "ToolApproved", {"capability": "fs.read"})
    assert service.build_evidence(ctx.session_id, None)["approvals"] == ["fs.read"]


def test_changed_durable_history_invalidates_inflight_summary(app_ctx):
    from rinari.storage.records import SessionMessageRecord

    service, ctx, caller, _calls, rebuild = setup(app_ctx)
    changed = []

    def invoke(_request):
        if not changed:
            app_ctx.message_repo.append_many(
                ctx.session_id,
                [
                    SessionMessageRecord(
                        id="new-source",
                        session_id=ctx.session_id,
                        seq=0,
                        role="user",
                        content="new evidence",
                    )
                ],
            )
            changed.append(True)
        return ModelResponse(content="summary")

    caller.invoke = invoke
    original = list(ctx.history)
    with pytest.raises(ContextPreparationError, match="Context changed"):
        prepare(service, ctx, rebuild(ctx), caller, rebuild, lambda *_: None, CancellationToken())
    assert ctx.history == original
    assert not app_ctx.session_repo.get(ctx.session_id).compact_state
