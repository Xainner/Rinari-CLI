"""Lote 3: the request prefix stays byte-identical between calls and turns.

Real PROJECT sessions lost the provider prompt cache between consecutive
turns: the task graph, the query-ranked memory and the repository scan sat in
the system prompt, before the history, so any change there invalidated the
cached prefix of the whole conversation. They now close the request.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.models.types import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
)
from rinari.prompts.assembler import AssemblerContext, PromptAssembler

_EPHEMERAL = {"type": "ephemeral"}


def _note(text: str = "## task-state\nTask graph: one") -> ChatMessage:
    bundle = PromptAssembler().build(AssemblerContext(task_state=text))
    note = bundle.turn_context_message
    assert note is not None
    return note


def _anthropic(messages) -> dict:
    from rinari.providers.adapters.anthropic import AnthropicAdapter

    return AnthropicAdapter()._payload(
        ModelRequest(model="claude-x", messages=messages), stream=True
    )


def test_anthropic_keeps_the_cache_breakpoint_before_the_turn_note() -> None:
    owner = ChatMessage.user("fix the tests")
    payload = _anthropic((ChatMessage.system("rules"), owner, _note()))
    last = payload["messages"][-1]
    assert last["role"] == "user" and len(payload["messages"]) == 1
    owner_block, note_block = last["content"]
    assert owner_block == {"type": "text", "text": "fix the tests", "cache_control": _EPHEMERAL}
    assert "cache_control" not in note_block and "Task graph" in note_block["text"]
    # The system prompt stays one cached block without the note.
    assert payload["system"] == [{"type": "text", "text": "rules", "cache_control": _EPHEMERAL}]


def test_anthropic_next_call_reads_the_prefix_the_previous_one_wrote() -> None:
    owner = ChatMessage.user("fix the tests")
    call = ToolCall("c1", "fs.read", {"path": "a.py"})
    first = _anthropic((ChatMessage.system("rules"), owner, _note("one")))
    second = _anthropic(
        (
            ChatMessage.system("rules"),
            owner,
            ChatMessage.assistant("Reading.", (call,)),
            ChatMessage.tool_result("c1", "fs.read", "print(1)"),
            _note("two"),
        )
    )
    # The block the first call cached is the same block here (a string is the
    # API's shorthand for one text block).
    written = first["messages"][0]["content"][0]
    assert {k: v for k, v in written.items() if k != "cache_control"} == {
        "type": "text",
        "text": second["messages"][0]["content"],
    }
    results, note = second["messages"][-1]["content"]
    assert results["type"] == "tool_result" and results["cache_control"] == _EPHEMERAL
    assert note["type"] == "text" and "two" in note["text"]


def test_anthropic_note_after_an_assistant_turn_opens_its_own_user_turn() -> None:
    from rinari.providers.adapters.anthropic import _append_turn_context

    payload = {"messages": [{"role": "assistant", "content": "hola"}]}
    _append_turn_context(payload, ["state"])
    assert payload["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "state"}],
    }


@pytest.mark.parametrize("transport", ["chat", "responses"])
def test_openai_transports_send_the_note_as_the_last_user_message(transport) -> None:
    request = ModelRequest(
        model="m",
        messages=(ChatMessage.system("rules"), ChatMessage.user("hola"), _note()),
    )
    if transport == "chat":
        from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter

        items = OpenAICompatibleAdapter()._payload(request, stream=True)["messages"]
        assert items[0] == {"role": "system", "content": "rules"}
    else:
        from rinari.providers.adapters.subscriptions import CodexResponsesAdapter

        payload = CodexResponsesAdapter()._responses_payload(request, stream=True)
        assert payload["instructions"] == "rules"
        items = payload["input"]
    assert items[-2]["content"] == "hola"
    assert items[-1]["role"] == "user" and "<turn-context>" in items[-1]["content"]


# -- end to end: a PROJECT session, one runtime per turn (as the desktop) -----


@dataclass
class _Model:
    scripted: list
    requests: list = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if "conversation title" in (request.messages[0].content or ""):
            return ModelResponse(content="Title", stop_reason=StopReason.END_TURN)
        self.requests.append(request)
        return self.scripted.pop(0)


@pytest.fixture
def project(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    root = user_home / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (root / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    services = build_services(app_ctx, user_home=user_home)
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
    services.memory.remember_user("Prefers concise answers", topic="style")
    services.memory.remember_project(str(root), "Tests use pytest", topic="tests")
    record = services.sessions.start(root).session
    assert record.kind == "PROJECT"
    return services, user_home, root, record


def _wire(messages) -> list[tuple]:
    """What a provider receives of each message (display fields stay local)."""
    return [(m.role, m.content, m.tool_calls, m.tool_call_id, m.name) for m in messages]


def _turn(services, user_home, record, monkeypatch, message, scripted):
    model = _Model(scripted)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *args: model)
    session = agent_runtime.build_agent_session(
        services, services.ctx.session_repo.get(record.id), interactive=False, user_home=user_home
    )
    agent_runtime.run_turn(session, message)
    return model.requests


def test_consecutive_project_turns_share_the_prefix_up_to_the_history(project, monkeypatch) -> None:
    services, user_home, root, record = project
    listing = ModelResponse(
        content="Listing.",
        tool_calls=(ToolCall("l1", "fs.list", {"path": str(root)}),),
    )
    done = ModelResponse(content="Done.", stop_reason=StopReason.END_TURN)
    first = _turn(services, user_home, record, monkeypatch, "add a web module", [listing, done])

    # The work between turns changes everything that used to sit in the prefix.
    (root / "web").mkdir()
    for index in range(3):
        (root / "web" / f"m{index}.ts").write_text("export {}\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    services.ctx.task_repo.create(
        {
            "id": "task_1",
            "project_root": str(root),
            "session_ref": record.id,
            "title": "Ship the web module",
            "description": "",
            "status": "in_progress",
            **{
                key: "[]"
                for key in (
                    "acceptance",
                    "implementation",
                    "validation",
                    "scope",
                    "unresolved",
                    "depends_on",
                    "blockers",
                    "evidence",
                )
            },
            "created_at": "2026-10-08T00:00:00Z",
            "updated_at": "2026-10-08T00:00:00Z",
        }
    )
    second = _turn(services, user_home, record, monkeypatch, "now run pytest", [done])

    previous, current = first[-1].messages, second[0].messages
    # System prompt identical; the history the previous call sent is replayed
    # unchanged; only the note that closes each request differs.
    assert previous[0] == current[0]
    assert previous[-1].is_turn_context and current[-1].is_turn_context
    assert _wire(current[1 : len(previous) - 1]) == _wire(previous[1:-1])
    assert sum(m.is_turn_context for m in current) == 1
    # Within a turn every call shares it too.
    assert first[0].messages[0] == first[-1].messages[0]

    # The volatile state still reaches the model, in its current version.
    note = current[-1].content
    assert "Ship the web module" in note and "typescript" in note
    assert "Tests use pytest" in note
    assert "Ship the web module" not in previous[-1].content
    # And it never becomes history.
    stored = services.ctx.message_repo.list(record.id)
    assert not any("<turn-context>" in (row.content or "") for row in stored)
