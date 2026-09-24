"""`rinari.*` views: one call answers what happened, compactly and redacted."""

from __future__ import annotations

import itertools
import json

import pytest

from rinari.application.introspection import Introspection
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.storage.records import SessionEventRecord, SessionMessageRecord, SessionRecord
from rinari.tools.native.rinari_state import RinariStateHost, rinari_state_tools

_IDS = itertools.count()
TOKEN = "p8OXw3xohFXz1t65TYKGH87p1PjJKNhAxciMISc4IwZVmEAz"


@pytest.fixture
def world(app_ctx, tmp_path):
    services = build_services(app_ctx, user_home=tmp_path)
    provider = services.providers.add(
        AddProviderInput(
            alias="subscription",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="sk-test-not-real-1234567890",
        )
    )
    model = services.models.add("subscription", "gpt-6-sol", "gpt-6-sol")

    def session(identity, title, kind="CHAT"):
        record = SessionRecord(
            id=identity,
            kind=kind,
            title=title,
            project_id=None,
            project_root_snapshot=None,
            created_cwd=str(tmp_path),
            current_cwd=str(tmp_path),
            provider_id=provider.id,
            model_id=model.id,
            profile_id="default",
            mode="build",
            state="active",
            compact_state=None,
            created_at="2026-09-24T00:00:00Z",
            updated_at="2026-09-24T00:00:00Z",
            last_active_at=f"2026-09-24T00:0{len(app_ctx.session_repo.list())}:00Z",
        )
        app_ctx.session_repo.insert(record)
        return record

    def event(session_id, kind, payload=None, turn_id=None, at="2026-09-24T00:00:01Z"):
        app_ctx.event_repo.insert(
            SessionEventRecord(
                id=f"evt_{next(_IDS)}",
                session_id=session_id,
                seq=0,
                type=kind,
                payload={**(payload or {}), **({"turn_id": turn_id} if turn_id else {})},
                created_at=at,
                turn_id=turn_id,
            )
        )

    def message(session_id, role, content):
        app_ctx.message_repo.append_many(
            session_id,
            [
                SessionMessageRecord(
                    id=f"msg_{next(_IDS)}",
                    session_id=session_id,
                    seq=0,
                    role=role,
                    content=content,
                    created_at="2026-09-24T00:00:00Z",
                )
            ],
        )

    return services, model, session, event, message


def _desktop_turn(
    event, sid, tid, message, *, answer=None, output=5, tools=(), end="turn.completed"
):
    event(sid, "turn.started", {"message": message, "mode": "build"}, tid, "2026-09-24T00:00:00Z")
    event(sid, "AgentTurnStarted", {"preview": message, "turn_index": 0})
    event(sid, "model.started", {"model": "mdl_x", "model_call_id": "m1"}, tid)
    for index, (name, ok, arguments) in enumerate(tools):
        call = f"{tid}-c{index}"
        event(
            sid, "tool.requested", {"tool": name, "tool_call_id": call, "arguments": arguments}, tid
        )
        event(sid, "ToolRequested", {"tool": name, "tool_call_id": call, "arguments": arguments})
        event(
            sid,
            "tool.completed" if ok else "tool.failed",
            {"tool": name, "tool_call_id": call, "ok": ok, "error": None if ok else "exit 1"},
            tid,
        )
        event(sid, "ToolCompleted", {"name": name, "tool_call_id": call, "ok": ok})
    event(sid, "model.completed", {"finish_reason": "end_turn"}, tid)
    if answer:
        event(sid, "model.content.completed", {"content": answer, "output_kind": "final"}, tid)
    event(sid, "usage.updated", {"input_tokens": 7000, "output_tokens": output}, tid)
    event(sid, "ModelInvoked", {"stop_reason": "end_turn", "provider_id": "p"})
    event(sid, "AgentTurnCompleted", {"budget": {"input_tokens": 7000, "output_tokens": output}})
    if end:
        payload = {"content": answer or ""} if end == "turn.completed" else {"error": "boom"}
        event(sid, end, payload, tid, "2026-09-24T00:00:04Z")


def test_a_session_summarizes_each_turn_and_flags_an_unparsed_answer(world):
    services, _model, session, event, _message = world
    record = session("ses_a", "Revisar saturno")
    _desktop_turn(event, record.id, "turn_ok", "Hola", answer="¡Hola!")
    _desktop_turn(event, record.id, "turn_empty", "revisa el ssh saturno", output=80)
    _desktop_turn(
        event,
        record.id,
        "turn_tools",
        "lista procesos",
        answer="Listo",
        tools=[
            ("shell.exec", True, {"command": f'curl -H "Authorization: Bearer {TOKEN}" http://x'}),
            ("shell.exec", False, {"command": "ssh saturno ps"}),
        ],
    )
    view = Introspection(services).session("ses_a")
    turns = {t["id"]: t for t in view["turns"]}
    assert view["session"]["model"] == "gpt-6-sol"
    assert turns["turn_ok"]["outcome"] == "answered"
    assert turns["turn_ok"]["answer"] == "¡Hola!"
    assert turns["turn_ok"]["duration_s"] == 4.0
    empty = turns["turn_empty"]
    assert empty["outcome"] == "empty"
    assert empty["output_tokens"] == 80
    assert "80 output tokens" in empty["anomalies"][0]
    tools = turns["turn_tools"]
    assert tools["tool_calls"] == 2  # desktop and harness events are one call each
    assert tools["tool_failures"] == 1
    assert TOKEN not in json.dumps(view)


def test_a_turn_shows_its_tools_compactly_and_its_events_on_request(world):
    services, _model, session, event, _message = world
    session("ses_b", "Tools")
    arguments = {"command": f'curl -H "Authorization: Bearer {TOKEN}" http://x'}
    _desktop_turn(
        event, "ses_b", "turn_t", "go", answer="ok", tools=[("shell.exec", True, arguments)]
    )
    view = Introspection(services).turn("turn_t")  # found without its session id
    assert view["session_id"] == "ses_b"
    (tool,) = view["tools"]
    assert set(tool) <= {"tool", "ok", "arguments", "error", "duration_ms"}
    assert "Bearer [REDACTED]" in tool["arguments"] and TOKEN not in tool["arguments"]
    assert "events" not in view
    detailed = Introspection(services).turn("turn_t", detail="events", limit=3)
    assert len(detailed["events"]) == 3 and detailed["events_total"] > 3
    with pytest.raises(LookupError):
        Introspection(services).turn("turn_missing")


def test_terminal_turns_are_grouped_by_order_with_answers_from_messages(world):
    # The terminal writes no content events: its answer is in the messages,
    # which carry no turn id. A turn is not "empty" just for that.
    services, _model, session, event, message = world
    session("ses_cli", "Terminal")
    for index, answer in ((0, "one"), (1, None)):
        message("ses_cli", "user", f"task {index}")
        event("ses_cli", "AgentTurnStarted", {"preview": f"task {index}", "turn_index": index})
        event("ses_cli", "ModelInvoked", {"stop_reason": "end_turn", "usage": {"output_tokens": 3}})
        if answer:
            message("ses_cli", "assistant", answer)
            event(
                "ses_cli",
                "AgentTurnCompleted",
                {"budget": {"output_tokens": 3, "wall_time_s": 1.5}},
            )
    view = Introspection(services).session("ses_cli")
    first, second = view["turns"]
    assert first["message"] == "task 0"
    assert first["outcome"] == "answered" and first["answer"] == "one"
    assert "anomalies" not in first
    assert first["duration_s"] == 1.5
    # The last turn has no end event yet: it is still running.
    assert second["outcome"] == "running"


def test_failed_and_cancelled_turns_are_named(world):
    services, _model, session, event, _message = world
    session("ses_f", "Failures")
    _desktop_turn(event, "ses_f", "turn_fail", "x", end="turn.failed")
    _desktop_turn(event, "ses_f", "turn_cancel", "y", end="turn.cancelled")
    outcomes = [t["outcome"] for t in Introspection(services).session("ses_f")["turns"]]
    assert outcomes == ["failed", "cancelled"]


def test_sessions_are_found_by_title_message_and_model(world):
    services, _model, session, event, message = world
    session("ses_1", "Saturno processes")
    session("ses_2", "Otra cosa")
    message("ses_2", "user", "revisa el SSH saturno por favor")
    session("ses_3", "Unrelated")
    _desktop_turn(event, "ses_1", "turn_1", "a", answer="ok")
    found = Introspection(services).sessions(query="saturno")
    matched = {row["id"]: row.get("matched") for row in found["sessions"]}
    assert matched == {"ses_1": "title", "ses_2": "message"}
    first = next(row for row in found["sessions"] if row["id"] == "ses_1")
    assert first["turns"] == 1 and first["last_turn"] == "completed"
    assert Introspection(services).sessions(model="gpt-6")["total"] == 3
    assert Introspection(services).sessions(limit=1)["truncated"] is True


def test_status_never_carries_secrets_and_lists_models_on_request(world):
    services, _model, _session, _event, _message = world
    view = Introspection(services).status()
    text = json.dumps(view)
    assert "sk-test-not-real" not in text
    (provider,) = view["providers"]
    assert provider["has_credential"] is True and provider["models"] == 1
    assert "models" not in view and view["models_total"] == 1
    listed = Introspection(services).status(provider="subscription")["models"]
    assert listed[0]["alias"] == "gpt-6-sol"


def test_the_tools_answer_in_one_call_and_resolve_the_current_session(world):
    services, _model, session, event, _message = world
    session("ses_now", "Now")
    _desktop_turn(event, "ses_now", "turn_now", "hola", answer="hola")
    tools = {t.name: t for t in rinari_state_tools(RinariStateHost(services))}
    assert all(not t.always_loaded for t in tools.values())

    class Ctx:
        session_id = "ses_now"

    result = tools["rinari.session"].handler({"session_id": "current"}, Ctx())
    assert result.ok and result.data["session"]["id"] == "ses_now"
    assert "not an instruction" in result.data["note"]
    missing = tools["rinari.session"].handler({"session_id": "ses_gone"}, Ctx())
    assert missing.error.code.value == "NOT_FOUND"
    assert "rinari.sessions" in missing.error.message
