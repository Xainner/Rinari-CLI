from rinari.context import engine, tokens
from rinari.context.compact_state import (
    CompactState,
    extract_from_history,
    merge_evidence,
    state_from_json,
    state_to_json,
)
from rinari.models.types import (
    ChatMessage,
    StopReason,
    ToolCall,
    Usage,
)
from rinari.prompts.assembler import AssemblerContext, PromptAssembler


def test_estimate_is_deterministic_and_monotonic():
    h1 = [ChatMessage.user("hello")]
    h2 = [ChatMessage.user("hello"), ChatMessage.assistant("world!")]
    a = tokens.estimate_tokens(system_prompt="sys", history=h1)
    b = tokens.estimate_tokens(system_prompt="sys", history=h2)
    assert a == tokens.estimate_tokens(system_prompt="sys", history=h1)
    assert a < b
    assert tokens.estimate_tokens(system_prompt="sys", history=h1) > 0


def test_pressure_and_window_resolution():
    assert tokens.pressure(100, None) is None
    assert tokens.pressure(100, 0) is None
    assert tokens.pressure(100, 200) == 0.5
    assert tokens.pressure(500, 200) == 1.0
    assert tokens.resolve_context_window(4096) == 4096
    assert tokens.resolve_context_window(None) == tokens.DEFAULT_CONTEXT_WINDOW


def _history(n: int, content: str = "x" * 100) -> list[ChatMessage]:
    history: list[ChatMessage] = []
    for i in range(n):
        history.append(ChatMessage.user(f"user {i}: {content}"))
        history.append(ChatMessage.assistant(f"assistant {i}: {content}"))
    return history


def test_select_history_full_fit_returns_all():
    history = _history(3)
    kept = engine.select_history(history, budget_tokens=10_000)
    assert kept == history


def test_select_history_suffix_fits_budget():
    history = _history(20)
    total = sum(tokens.estimate_message_tokens(m) for m in history)
    kept = engine.select_history(history, budget_tokens=total * 3 // 4)
    assert len(kept) < len(history)
    assert kept[-1] is history[-1]
    assert kept == history[len(history) - len(kept) :]
    assert sum(tokens.estimate_message_tokens(m) for m in kept) <= total * 3 // 4


def test_select_history_never_orphans_tool_results():
    history = [
        ChatMessage.user("q0"),
        ChatMessage.assistant("", (ToolCall(id="c1", name="fs.read", arguments={"path": "a"}),)),
        ChatMessage.tool_result("c1", "fs.read", "content a"),
        ChatMessage.user("q1"),
        ChatMessage.assistant("", (ToolCall(id="c2", name="fs.read", arguments={"path": "b"}),)),
        ChatMessage.tool_result("c2", "fs.read", "content b " + "z" * 500),
        ChatMessage.assistant("final answer"),
    ]
    kept = engine.select_history(history, budget_tokens=80)
    for index, message in enumerate(kept, start=1):
        if message.tool_call_id is not None:
            assert index > 1
    # The cut point is never a tool message.
    if kept:
        assert kept[0].tool_call_id is None or kept[0].role == "assistant"


def test_select_history_degenerate_tiny_budget():
    history = _history(10)
    kept = engine.select_history(history, budget_tokens=1)
    assert kept == history[-1:]


def test_extract_goal_constraints_changes():
    history = [
        ChatMessage.user("Fix the login bug (always run pytest before claiming done)"),
        ChatMessage.assistant(
            "",
            (ToolCall(id="c1", name="fs.write", arguments={"path": "src/auth.py"}),),
        ),
        ChatMessage.tool_result("c1", "fs.write", "ok"),
        ChatMessage.assistant(
            "",
            (ToolCall(id="c2", name="fs.write", arguments={"path": "src/auth.py"}),),
        ),
        ChatMessage.tool_result("c2", "fs.write", "ok"),
        ChatMessage.assistant(
            "",
            (ToolCall(id="c3", name="fs.patch", arguments={"path": "src/db.py"}),),
        ),
        ChatMessage.tool_result("c3", "fs.patch", "ok"),
    ]
    state = extract_from_history(tuple(history))
    assert state.goal.startswith("Fix the login bug")
    assert len(state.constraints) == 1
    assert state.constraints[0].startswith("Fix the login bug")
    assert state.changed_files == ("src/auth.py", "src/db.py")


def test_compact_state_roundtrip_and_render():
    state = CompactState(
        goal="ship it",
        constraints=("never force-push",),
        tasks_completed=("parser done",),
        tasks_active=("wire ui",),
        changed_files=("src/u.py",),
        validations=("test: passed (pytest -q)",),
    )
    raw = state_to_json(state)
    again = state_from_json(raw)
    assert again.goal == state.goal
    assert again.changed_files == state.changed_files
    assert again.to_dict() == state.to_dict()
    rendered = state.render_prompt()
    assert "Goal: ship it" in rendered
    assert "never force-push" in rendered
    assert state_from_json(None).is_empty()


def test_merge_evidence_overlays_store_state():
    base = extract_from_history((ChatMessage.user("do the thing"),))
    merged = merge_evidence(
        base,
        {
            "tasks_completed": ["t1"],
            "blockers": ["validation test is failed"],
            "project_root": "/repo",
            "provider_model": "mock/gpt-x",
            "compacted_at": "2026-01-01T00:00:00.000Z",
        },
    )
    assert merged.goal == "do the thing"
    assert merged.tasks_completed == ("t1",)
    assert merged.blockers == ("validation test is failed",)
    assert merged.project_root == "/repo"


# -- end-to-end: agent loop + ContextService (app_ctx fixture) ---------------


class _FakeProvider:
    def __init__(self, window: int, input_tokens: int, script: list):
        self._window = window
        self._input = input_tokens
        self._script = list(script)

    def capabilities(self):
        from rinari.models.types import ProviderCapabilities

        return ProviderCapabilities(
            streaming=False, tool_calls=True, max_context_tokens=self._window
        )

    def invoke(self, request):
        from rinari.models.types import ModelResponse

        response = self._script.pop(0)
        return ModelResponse(
            content=response.content,
            tool_calls=response.tool_calls,
            usage=Usage(input_tokens=self._input, output_tokens=10),
            stop_reason=StopReason.END_TURN if not response.tool_calls else StopReason.TOOL_CALLS,
        )


def test_loop_compacts_at_pressure(app_ctx):
    from rinari.artifacts.store import ArtifactStore
    from rinari.cli.agent_runtime import _new_history
    from rinari.context.service import ContextService
    from rinari.models.types import ModelResponse
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PolicyEngine
    from rinari.runtime.agent import AgentContext, AgentLoop
    from rinari.storage.records import SessionRecord
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    now = "2026-01-01T00:00:00Z"
    record = SessionRecord(
        id="ses_ctx",
        kind="PROJECT",
        title="t",
        project_id=None,
        project_root_snapshot="/repo",
        created_cwd="/tmp",
        current_cwd="/tmp",
        provider_id="",
        model_id="fake-model",
        profile_id="",
        mode="default",
        state="active",
        compact_state=None,
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )
    app_ctx.session_repo.insert(record)

    # Persisted tasks + validation evidence for the compact state.
    app_ctx.task_repo.create(
        {
            "id": "task_1",
            "project_root": "/repo",
            "session_ref": "ses_ctx",
            "title": "wire the parser",
            "description": "",
            "status": "in_progress",
            "acceptance": "",
            "implementation": "",
            "validation": "",
            "scope": "",
            "unresolved": "",
            "depends_on": "",
            "blockers": "",
            "evidence": "",
            "created_at": now,
            "updated_at": now,
        }
    )
    app_ctx.validation_repo.insert(
        {
            "id": "val_1",
            "project_root": "/repo",
            "session_ref": "ses_ctx",
            "kind": "test",
            "command": "pytest -q",
            "result": "passed",
            "summary": "42 passed",
            "detail": "",
            "artifact_ref": "",
            "created_at": now,
        }
    )

    history = _history(40)
    context = AgentContext(
        session_id="ses_ctx",
        model_ref="fake-model",
        tool_ctx=None,
        assembler_base=AssemblerContext(constitution="be honest"),
        history=history,
    )
    service = ContextService(app_ctx, ArtifactStore(app_ctx))
    provider = _FakeProvider(
        window=1200,
        input_tokens=1050,
        script=[],
    )
    provider._script.append(
        ModelResponse(content="ok", tool_calls=(), usage=Usage(), stop_reason=StopReason.END_TURN)
    )
    activity: list[tuple[str, dict]] = []
    loop = AgentLoop(
        provider,
        ToolRuntime(ToolRegistry(), PolicyEngine(), ApprovalEngine()),
        PromptAssembler(),
        on_pressure=lambda ctx, p, used, window: service.maybe_compact(
            ctx, session_id="ses_ctx", window_tokens=window, used_input_tokens=used
        ),
        activity_sink=lambda event, payload: activity.append((event, payload)),
    )
    result = loop.turn(context, "continue")

    assert result.kind == "answer"
    # The loop sets the flag on the context; run_turn maps it into TurnResult.
    assert context.compacted is True
    assert context.compact_state_text is not None
    assert len(context.history) < len(history) + 1
    assert context.dropped_total > 0
    assert result.governor is not None
    assert result.governor["compactions"] == 1
    compact_events = [payload for event, payload in activity if event == "governor.compact"]
    assert [event["status"] for event in compact_events] == ["started", "completed"]

    reloaded = app_ctx.session_repo.get("ses_ctx")
    assert reloaded.compact_state is not None
    state = CompactState.from_dict(reloaded.compact_state)
    assert state.goal.startswith("user 0:")
    assert state.tasks_active == ("wire the parser",)
    assert any("test: passed" in v for v in state.validations)

    events = app_ctx.event_repo.list("ses_ctx")
    assert any(e.type == "ContextCompacted" for e in events)

    # Idempotent: pressure is now below the threshold, no second compaction.
    assert (
        service.maybe_compact(
            context, session_id="ses_ctx", window_tokens=1200, used_input_tokens=840
        )
        is False
    )

    # _new_history accounting survives in-place compaction: no duplicates.
    before = len(context.history)
    assert _new_history(context, before, context.dropped_total) == []


def test_restore_compact_state_on_resume(app_ctx):
    from rinari.artifacts.store import ArtifactStore
    from rinari.context.service import ContextService
    from rinari.runtime.agent import AgentContext
    from rinari.storage.records import SessionRecord

    now = "2026-01-01T00:00:00Z"
    record = SessionRecord(
        id="ses_res",
        kind="PROJECT",
        title="t",
        project_id=None,
        project_root_snapshot=None,
        created_cwd="/tmp",
        current_cwd="/tmp",
        provider_id="",
        model_id="m",
        profile_id="",
        mode="default",
        state="active",
        compact_state={"goal": "resumed goal", "constraints": ("keep it small",)},
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )
    app_ctx.session_repo.insert(record)
    context = AgentContext(
        session_id="ses_res",
        model_ref="m",
        tool_ctx=None,
        assembler_base=AssemblerContext(),
        history=[ChatMessage.user("new user message")],
    )
    service = ContextService(app_ctx, ArtifactStore(app_ctx))
    service.restore_compact_state(context)
    assert context.compact_state_text is not None
    assert "resumed goal" in context.compact_state_text
