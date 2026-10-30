"""The conversation's tool list and settled boundary survive the per-turn
runtime the desktop builds (F42, F45)."""

from __future__ import annotations

from dataclasses import replace

from rinari.application.services import build_services
from rinari.cli.agent_runtime import _exposure_for, _tools_note, build_agent_session
from rinari.context.session_state import SessionState, state_path
from rinari.context.settle import SettleState
from rinari.models.types import ChatMessage


def _session(app_ctx, tmp_path):
    from rinari.application.provider_service import AddProviderInput

    services = build_services(app_ctx)
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
    cwd = tmp_path / "work"
    cwd.mkdir()
    return services, services.sessions.start(cwd, forced_chat=True).session


def test_the_list_decided_once_is_what_every_later_runtime_sends(app_ctx, tmp_path):
    services, record = _session(app_ctx, tmp_path)
    first = build_agent_session(services, record, interactive=False)
    registry = first.loop._tools.registry
    listed = first.context.tool_ctx.exposure.for_model(registry)
    assert "tool.call" in {s.name for s in listed}
    assert len(listed) < 128
    first.end()

    # The next turn's runtime sends the saved list, even if the registry moved.
    second = build_agent_session(services, record, interactive=False)
    registry = second.loop._tools.registry
    registry.register(replace(registry.get("fs.read"), description="changed in between"))
    assert second.context.tool_ctx.exposure.for_model(registry) == listed
    second.end()

    # Another model or a compaction decides it again.
    other = replace(record, model_id="mdl_other")
    assert _exposure_for(services, other, None).fixed is None
    compacted = replace(record, compact_state={"revision": 1})
    assert _exposure_for(services, compacted, None).fixed is None
    assert _exposure_for(services, record, None).fixed == listed


def test_a_turn_tool_outside_the_list_is_announced_once(app_ctx, tmp_path):
    services, record = _session(app_ctx, tmp_path)
    session = build_agent_session(services, record, interactive=False)
    exposure = session.context.tool_ctx.exposure
    registry = session.loop._tools.registry
    assert not exposure.in_list("project.instructions.propose", registry)
    # Nothing to announce by default: a tool that needs a service the session
    # lacks (goal.update without a goal) is not "newly available".
    exposure.for_model(registry)
    assert _tools_note(session) is None

    note = _tools_note(session, ("project.instructions.propose", "fs.read"))
    assert note is not None and note.origin == {"kind": "harness", "source": "tools"}
    assert "- project.instructions.propose:" in note.content
    assert "call: tool.call {" in note.content
    assert "- fs.read:" not in note.content  # already in the list
    # Same text every time, and said once per conversation.
    assert _tools_note(session, ("project.instructions.propose",)).content == note.content
    session.context.history.append(note)
    assert _tools_note(session, ("project.instructions.propose",)) is None
    session.end()


def test_the_turn_note_follows_the_owner_message(app_ctx, tmp_path):
    from rinari.models.types import ModelResponse
    from tests.unit.test_agent_loop import FakeModel

    services, record = _session(app_ctx, tmp_path)
    session = build_agent_session(services, record, interactive=False)
    note = ChatMessage.harness("tools note", "tools")
    session.context.turn_notes = (note,)
    model = FakeModel(scripted=[ModelResponse(content="ok")])
    session.loop._provider = model
    session.loop.turn(session.context, "hello")
    roles = [(m.role, m.content) for m in session.context.history]
    assert roles[:2] == [("user", "hello"), ("user", "tools note")]
    assert session.context.turn_notes == ()
    session.end()


def test_the_settled_boundary_survives_a_restored_history(tmp_path):
    history = [ChatMessage.user(f"m{i}") for i in range(6)]
    state = SettleState()
    state.move_to(4, history)
    store = SessionState(tmp_path / "s.state.json")
    store.put("settle", state.to_json())

    # The next turn restores the same messages as new objects.
    restored = [replace(m) for m in history]
    again = SettleState.from_json(store.get("settle"))
    assert again.current(restored) == 4
    # A history rewritten by compaction starts over.
    rewritten = [ChatMessage.user("summary"), *restored[3:]]
    assert again.current(rewritten) == 0


def test_deleting_a_session_drops_its_state(app_ctx, tmp_path):
    services, record = _session(app_ctx, tmp_path)
    path = state_path(services.ctx.layout, record.id)
    SessionState(path).put("tools", {"key": {}, "schemas": []})
    assert path.exists()
    services.sessions.delete(record.id)
    assert not path.exists()
