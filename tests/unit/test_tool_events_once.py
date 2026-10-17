"""Each tool call is stored once (plan 0.4.0 #91).

The Engine stores the activity events (`tool.requested` with the whole
arguments in `full_arguments`, and `tool.completed`); the runtime's
`ToolRequested` / `ToolCompleted` were a second copy and are no longer
written there. The CLI, which stores no activity, keeps them. Readers count
each call once, from old sessions and new.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
)
from rinari.runtime import tool_events
from rinari.runtime.durable_history import recover_legacy_turns


class Scripted:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.requests: list[ModelRequest] = []

    def capabilities(self):
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request):
        self.requests.append(request)
        return self.scripted.pop(0)


@pytest.fixture
def server(app_ctx, tmp_path, monkeypatch):
    user_home = tmp_path / "home"
    user_home.mkdir()
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
    model = Scripted(
        [
            ModelResponse(
                content="Leo el archivo.",
                tool_calls=(ToolCall("c1", "fs.read", {"path": "nota.txt"}),),
                stop_reason=StopReason.TOOL_CALLS,
            ),
            ModelResponse(content="Listo.", stop_reason=StopReason.END_TURN),
        ]
    )
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, record: model)
    engine = EngineServer(services, user_home=user_home)
    yield engine
    engine.close()


def _call(server, method, params):
    response = server.handle_line(json.dumps({"id": method, "method": method, "params": params}))
    assert response is not None and response["ok"], response
    return response["result"]


def test_the_engine_stores_each_call_once_with_its_whole_arguments(server, tmp_path) -> None:
    (tmp_path / "nota.txt").write_text("hola", encoding="utf-8")
    sid = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["session"]["id"]
    _call(server, "session.turn.start", {"session_id": sid, "message": "lee nota.txt"})
    deadline = time.time() + 20
    while server.turns.has_active_turn(sid) and time.time() < deadline:
        time.sleep(0.05)

    events = server._services.ctx.event_repo.list(sid)
    types = [event.type for event in events]
    assert "ToolRequested" not in types and "ToolCompleted" not in types
    [requested] = [event.payload for event in events if event.type == "tool.requested"]
    assert requested["full_arguments"] == {"path": "nota.txt"}
    assert isinstance(requested["turn_index"], int)
    assert types.count("tool.completed") == 1

    # Readers still see the call, once.
    [request] = tool_events.requests(events)
    assert request["arguments"] == {"path": "nota.txt"} and request["tool"] == "fs.read"
    report = _call(server, "usage.report", {"scope": "session", "ref": sid})["report"]
    assert report["tools"]["total"] == 1 and report["tools"]["ok"] == 1
    # The page shows the trimmed display copy, never the whole record.
    timeline = _call(server, "session.timeline", {"ref": sid})
    items = [item for turn in timeline["turns"] for item in turn["items"]]
    assert all("full_arguments" not in item for item in items)


def test_old_sessions_with_both_copies_count_once() -> None:
    events = [
        ("ToolRequested", {"tool_call_id": "c", "tool": "fs.read", "arguments": {"path": "a"}}),
        ("tool.requested", {"tool_call_id": "c", "tool": "fs.read", "arguments": '{"path"'}),
        ("ToolCompleted", {"tool_call_id": "c", "name": "fs.read", "ok": True}),
        ("tool.completed", {"tool_call_id": "c", "tool": "fs.read", "ok": True}),
        ("tool.failed", {"tool_call_id": "d", "tool": "shell.exec", "ok": False}),
        ("ToolCompleted", {"name": "checklist.update", "ok": True}),
    ]
    assert [item["arguments"] for item in tool_events.requests(events)] == [{"path": "a"}]
    ends = tool_events.ends(events)
    assert [(item["tool"], item["ok"]) for item in ends] == [
        ("fs.read", True),
        ("shell.exec", False),
    ]


def test_a_failed_turn_is_recovered_from_the_single_stored_request() -> None:
    records = [SimpleNamespace(role="user", turn_id="t")]
    pairs = [
        ("turn.started", {"turn_id": "t"}),
        (
            "tool.requested",
            {
                "tool_call_id": "c",
                "tool": "shell.exec",
                "arguments": '{"argv": ["x"]}',
                "full_arguments": {"argv": ["x"]},
            },
        ),
        ("tool.completed", {"tool_call_id": "c", "presentation": {"exit_code": 1}}),
        ("turn.failed", {}),
    ]
    events = [{"type": kind, "payload_json": json.dumps(data)} for kind, data in pairs]
    recovered = recover_legacy_turns(records, events)["t"]
    assert recovered[0].tool_calls[0].arguments == {"argv": ["x"]}
