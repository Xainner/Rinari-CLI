"""Engine Protocol slice 4a: session.history over persisted conversation rows."""

from __future__ import annotations

import json
import time

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
)
from rinari.storage.records import SessionMessageRecord


class _AnswerModel:
    """Minimal scripted caller for the live-persistence test."""

    def __init__(self, scripted: list[ModelResponse]) -> None:
        self.scripted = scripted

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.scripted.pop(0)


def _collect_until(server, session_id, timeout=30.0):
    terminal = {"turn.completed", "turn.cancelled", "turn.failed"}
    seen: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in terminal:
                return seen
        time.sleep(0.02)
    return seen


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    container.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    container.models.add("fake", "fake-model-1", "fake-one")
    container.providers.use("fake")
    return container


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def _req(request_id, method, params=None):
    line: dict = {"id": request_id, "method": method}
    if params is not None:
        line["params"] = params
    return json.dumps(line)


def _create_chat(server, tmp_path):
    response = server.handle_line(
        _req("h-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _seed(services, session_id, roles):
    records = [
        SessionMessageRecord(
            id=f"msg-{session_id}-{index}",
            session_id=session_id,
            seq=0,
            role=role,
            content=f"text-{index}",
            tool_calls=[{"id": "tc-1", "name": "read", "arguments": "{}"}]
            if role == "assistant" and index == 1
            else None,
            created_at="2026-01-01T00:00:00Z",
        )
        for index, role in enumerate(roles)
    ]
    services.ctx.message_repo.append_many(session_id, records)


def test_history_empty_for_new_session(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(_req("h1", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert result["session_id"] == session_id
    assert result["messages"] == []
    assert result["total"] == 0
    assert result["has_more"] is False


def test_history_returns_rows_in_order(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id, ["user", "assistant", "user"])
    response = server.handle_line(_req("h2", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    messages = response["result"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [m["content"] for m in messages] == ["text-0", "text-1", "text-2"]
    assert [m["seq"] for m in messages] == [1, 2, 3]
    assert messages[1]["tool_calls"] == [{"id": "tc-1", "name": "read", "arguments": "{}"}]
    assert messages[0]["tool_calls"] is None
    assert response["result"]["total"] == 3
    assert response["result"]["has_more"] is False


def test_history_limit_returns_tail_window(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id, ["user"] * 5)
    response = server.handle_line(_req("h3", "session.history", {"ref": session_id, "limit": 2}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert [m["seq"] for m in result["messages"]] == [4, 5]
    assert result["total"] == 5
    assert result["has_more"] is True


def test_history_pages_back_with_a_cursor(server, services, tmp_path) -> None:
    """#70: one page at a time, reading only that page, with «load earlier»."""
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id, ["user"] * 7)
    first = server.handle_line(_req("h6", "session.history", {"ref": session_id, "limit": 3}))
    assert first is not None and first["ok"] is True
    assert [m["seq"] for m in first["result"]["messages"]] == [5, 6, 7]
    assert first["result"]["next_before_seq"] == 5
    second = server.handle_line(
        _req("h7", "session.history", {"ref": session_id, "limit": 3, "before_seq": 5})
    )
    assert second is not None and second["ok"] is True
    assert [m["seq"] for m in second["result"]["messages"]] == [2, 3, 4]
    last = server.handle_line(
        _req("h8", "session.history", {"ref": session_id, "limit": 3, "before_seq": 2})
    )
    assert last is not None and last["ok"] is True
    assert [m["seq"] for m in last["result"]["messages"]] == [1]
    assert last["result"]["has_more"] is False
    assert last["result"]["next_before_seq"] is None
    assert last["result"]["total"] == 7


def test_history_page_cost_does_not_grow_with_the_conversation(server, services, tmp_path) -> None:
    """#70: 5000 messages open as fast as 100: only the page is read."""
    small = _create_chat(server, tmp_path)
    _seed(services, small, ["user"] * 100)
    big = server.handle_line(_req("h-big", "session.create", {"cwd": str(tmp_path), "chat": True}))[
        "result"
    ]["session"]["id"]
    _seed(services, big, ["user"] * 5000)

    def timed(ref: str) -> float:
        best = 1e9
        for attempt in range(5):
            start = time.perf_counter()
            response = server.handle_line(
                _req(f"h-t-{ref}-{attempt}", "session.history", {"ref": ref, "limit": 100})
            )
            best = min(best, time.perf_counter() - start)
            assert response is not None and response["ok"] is True
        return best

    assert timed(big) < timed(small) * 4 + 0.05


def test_history_unknown_session_is_not_found(server) -> None:
    response = server.handle_line(_req("h4", "session.history", {"ref": "ses-nope"}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_history_rejects_bad_limit(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(_req("h5", "session.history", {"ref": session_id, "limit": 0}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMS"


def test_protocol_turn_persists_messages(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: _AnswerModel(
            scripted=[ModelResponse(content="hola", stop_reason=StopReason.END_TURN)]
        ),
    )
    started = server.handle_line(
        _req("h6", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    events = _collect_until(server, session_id)
    assert events and events[-1]["event"] == "turn.completed"

    deadline = time.time() + 10
    history = None
    while time.time() < deadline:
        response = server.handle_line(_req("h7", "session.history", {"ref": session_id}))
        assert response is not None and response["ok"] is True
        if response["result"]["total"] >= 2:
            history = response["result"]["messages"]
            break
        time.sleep(0.05)
    assert history is not None, "protocol turn must persist user + assistant rows"
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hi"
    assert history[-1]["role"] == "assistant"
    assert "hola" in (history[-1]["content"] or "")


def test_timeline_page_reads_only_its_turns_and_stays_light(server, services, tmp_path) -> None:
    """#91: a conversation of 10 000 events opens with a small, fast page.

    Only the page's turns are read; the text the model saw (`observation`) and
    the live chunks of a tool that ended stay out.
    """
    from rinari.storage.records import SessionEventRecord

    session_id = _create_chat(server, tmp_path)
    events = []
    for index in range(400):
        turn_id = f"turn-{index}"
        kinds = [("turn.started", {"message": f"ask-{index}"})]
        kinds += [
            (
                "tool.output.delta",
                {"tool_call_id": f"call-{index}", "stream": "stdout", "delta": "z" * 200},
            )
            for _ in range(20)
        ]
        kinds += [
            (
                "tool.completed",
                {
                    "tool_call_id": f"call-{index}",
                    "tool": "shell.exec",
                    "observation": "o" * 20_000,
                    "presentation": {"kind": "command", "stdout": "s" * 50_000},
                },
            ),
            ("turn.completed", {}),
        ]
        events += [
            SessionEventRecord(
                id=f"evt-{index}-{position}",
                session_id=session_id,
                seq=0,
                type=kind,
                payload=payload,
                created_at="2026-01-01T00:00:00Z",
                turn_id=turn_id,
            )
            for position, (kind, payload) in enumerate(kinds)
        ]
    with services.ctx.db.transaction():
        for record in events:
            services.ctx.event_repo.insert(record)

    start = time.perf_counter()
    response = server.handle_line(
        _req("tl-big", "session.timeline", {"ref": session_id, "limit": 30})
    )
    elapsed = time.perf_counter() - start
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert [turn["turn_index"] for turn in result["turns"]] == list(range(370, 400))
    assert result["has_more"] is True and result["next_before_turn_index"] == 370
    items = [item for turn in result["turns"] for item in turn["items"]]
    assert not any(item["event"] == "tool.output.delta" for item in items)
    completed = [item for item in items if item["event"] == "tool.completed"]
    assert completed and all("observation" not in item for item in completed)
    # The page's cut, not the tool's: the whole output is one call away.
    assert all(item["presentation"]["page_truncated"] is True for item in completed)
    assert all("truncated" not in item["presentation"] for item in completed)
    whole = server.handle_line(
        _req(
            "tl-full",
            "session.tool.output",
            {"ref": session_id, "tool_call_id": completed[0]["tool_call_id"]},
        )
    )
    assert whole is not None and whole["ok"] is True
    assert len(whole["result"]["stdout"]) == 50_000
    missing = server.handle_line(
        _req("tl-none", "session.tool.output", {"ref": session_id, "tool_call_id": "nope"})
    )
    assert missing is not None and missing["error"]["code"] == "NOT_FOUND"
    assert len(json.dumps(response)) < 500_000
    assert elapsed < 1.0

    earlier = server.handle_line(
        _req(
            "tl-earlier",
            "session.timeline",
            {"ref": session_id, "limit": 30, "before_turn_index": 370},
        )
    )
    assert earlier is not None and earlier["ok"] is True
    assert [turn["turn_index"] for turn in earlier["result"]["turns"]] == list(range(340, 370))


def test_an_ended_tool_drops_its_live_chunks(server, services, tmp_path) -> None:
    """#91: the terminal event carries the output; the chunks of another tool stay."""
    from rinari.storage.records import SessionEventRecord

    session_id = _create_chat(server, tmp_path)
    repo = services.ctx.event_repo
    for index, call in enumerate(["a", "a", "b"]):
        repo.insert(
            SessionEventRecord(
                id=f"chunk-{index}",
                session_id=session_id,
                seq=0,
                type="tool.output.delta",
                payload={"tool_call_id": call, "delta": "x"},
                created_at="2026-01-01T00:00:00Z",
                turn_id="turn-1",
            )
        )
    assert repo.compact_tool_output(session_id, "turn-1", "a") == 2
    left = [
        row.payload["tool_call_id"] for row in repo.list(session_id, types=["tool.output.delta"])
    ]
    assert left == ["b"]


def test_a_busy_page_stays_light_and_its_newest_turn_comes_whole(server, services, tmp_path):
    """#91: a turn of 30 commands each weighed megabytes per page. The page
    keeps under 500 KB with older turns behind «Cargar anteriores», the
    newest turn always comes whole, and what was cut is one call away."""
    from rinari.storage.records import SessionEventRecord

    session_id = _create_chat(server, tmp_path)
    final = "fin " * 5_000  # 20 000 characters: never cut, it is the answer
    events = []
    seq = 0
    for index in range(6):
        turn_id = f"busy-{index}"
        rows = [("turn.started", {"message": f"ask-{index}"})]
        for call in range(40):
            call_id = f"c-{index}-{call}"
            rows += [
                (
                    "tool.requested",
                    {
                        "tool_call_id": call_id,
                        "tool": "fs.write",
                        "arguments": json.dumps({"path": "src/a.py", "content": "x" * 5_000}),
                    },
                ),
                ("tool.started", {"tool_call_id": call_id, "arguments": "{" + "y" * 5_000}),
                (
                    "tool.completed",
                    {
                        "tool_call_id": call_id,
                        "tool": "fs.write",
                        "presentation": {"kind": "tool", "data": {"content": "z" * 9_000}},
                    },
                ),
            ]
        rows.append(("model.content.completed", {"content": final, "output_kind": "final"}))
        rows.append(("turn.completed", {}))
        for kind, payload in rows:
            seq += 1
            events.append(
                SessionEventRecord(
                    id=f"busy-{seq}",
                    session_id=session_id,
                    seq=seq + 100,
                    type=kind,
                    payload=payload,
                    created_at="2026-01-01T00:00:00Z",
                    turn_id=turn_id,
                )
            )
    with services.ctx.db.transaction():
        for record in events:
            services.ctx.event_repo.insert(record)

    response = server.handle_line(_req("busy", "session.timeline", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert len(json.dumps(result)) < 500_000
    assert result["has_more"] is True and result["turns"][-1]["turn_id"] == "busy-5"
    newest = result["turns"][-1]
    assert newest["final_response"] == final
    items = newest["items"]
    requested = next(item for item in items if item["event"] == "tool.requested")
    # Still JSON: the path stays whole for the row's label, the content is cut.
    arguments = json.loads(requested["arguments"])
    assert arguments["path"] == "src/a.py" and len(arguments["content"]) < 400
    assert all("arguments" not in item for item in items if item["event"] == "tool.started")
    completed = next(item for item in items if item["event"] == "tool.completed")
    assert completed["presentation"]["page_truncated"] is True
    whole = server.handle_line(
        _req(
            "busy-whole",
            "session.tool.output",
            {"ref": session_id, "tool_call_id": completed["tool_call_id"]},
        )
    )
    assert whole is not None and len(whole["result"]["data"]["content"]) == 9_000
