"""Every protocol line fits the desktop host: bounded pages and oversized frames."""

from __future__ import annotations

import io
import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol import frames
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.transports import stdio
from rinari.storage.records import SessionEventRecord, SessionMessageRecord

MIB = 1024 * 1024


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


def _chat(server, tmp_path, request_id="c"):
    response = server.handle_line(
        _req(request_id, "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _messages(services, session_id, sizes):
    services.ctx.message_repo.append_many(
        session_id,
        [
            SessionMessageRecord(
                id=f"msg-{index}",
                session_id=session_id,
                seq=0,
                role="tool" if index % 2 else "user",
                content=f"start-{index} " + "x" * size,
                created_at="2026-01-01T00:00:00Z",
            )
            for index, size in enumerate(sizes)
        ],
    )


def _turn(services, session_id, index, output_size):
    turn_id = f"turn-{index}"
    for kind, payload in (
        ("turn.started", {"message": f"ask-{index}"}),
        ("tool.completed", {"tool": "fs.read", "result": "y" * output_size}),
        ("turn.completed", {}),
    ):
        services.ctx.event_repo.insert(
            SessionEventRecord(
                id=f"evt-{index}-{kind}",
                session_id=session_id,
                seq=0,
                type=kind,
                payload=payload,
                created_at="2026-01-01T00:00:00Z",
                turn_id=turn_id,
            )
        )


# -- helpers -------------------------------------------------------------------


def test_fit_cuts_long_strings_and_never_a_data_url_in_half() -> None:
    value = {"text": "a" * (3 * MIB), "image": "data:image/jpeg;base64," + "b" * (3 * MIB)}
    reduced = frames.fit(value, 2 * MIB)
    assert frames.encoded_size(reduced) <= 2 * MIB
    assert reduced["text"].startswith("a" * 1000)
    assert "characters omitted" in reduced["text"]
    assert reduced["image"].startswith("[image omitted")
    assert frames.fit({"small": "ok"}, 100) == {"small": "ok"}


def test_newest_within_keeps_the_newest_items_in_order() -> None:
    items = [{"n": index, "body": "z" * MIB} for index in range(5)]
    kept = frames.newest_within(items, 3 * MIB)
    assert [item["n"] for item in kept] == [3, 4]
    single = frames.newest_within([{"n": 0, "body": "z" * (5 * MIB)}], MIB)
    assert single[0]["n"] == 0
    assert frames.encoded_size(single) <= MIB


# -- bounded pages ---------------------------------------------------------------


def test_history_page_fits_the_budget_and_reports_older_messages(
    server, services, tmp_path
) -> None:
    session_id = _chat(server, tmp_path)
    _messages(services, session_id, [3 * MIB] * 8)
    response = server.handle_line(_req("h", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert len(json.dumps(response)) <= frames.PAGE_BUDGET_BYTES + 4096
    assert result["messages"][-1]["content"].startswith("start-7 ")
    assert 0 < len(result["messages"]) < 8
    assert result["total"] == 8 and result["has_more"] is True


def test_history_with_one_huge_message_still_opens(server, services, tmp_path) -> None:
    session_id = _chat(server, tmp_path)
    _messages(services, session_id, [20 * MIB])
    response = server.handle_line(_req("h", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    content = response["result"]["messages"][0]["content"]
    assert content.startswith("start-0 ") and "characters omitted" in content
    assert len(json.dumps(response)) < frames.MAX_FRAME_BYTES
    # Only the page is shortened; the stored message is intact.
    assert len(services.ctx.message_repo.list(session_id)[0].content) > 20 * MIB


def test_timeline_page_fits_the_budget_and_pages_back(server, services, tmp_path) -> None:
    session_id = _chat(server, tmp_path)
    for index in range(6):
        _turn(services, session_id, index, 3 * MIB)
    response = server.handle_line(_req("t", "session.timeline", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert len(json.dumps(response)) <= frames.PAGE_BUDGET_BYTES + 4096
    turns = result["turns"]
    assert turns[-1]["turn_id"] == "turn-5"
    assert result["has_more"] is True
    assert result["next_before_turn_index"] == turns[0]["turn_index"]
    older = server.handle_line(
        _req(
            "t2",
            "session.timeline",
            {"ref": session_id, "before_turn_index": result["next_before_turn_index"]},
        )
    )
    assert older is not None and older["ok"] is True
    assert older["result"]["turns"][-1]["turn_index"] == turns[0]["turn_index"] - 1


# -- stdio guard -------------------------------------------------------------------


def test_oversized_response_becomes_an_error_for_that_request(
    server, services, tmp_path, monkeypatch
) -> None:
    session_id = _chat(server, tmp_path)
    empty = _chat(server, tmp_path, "c2")
    _messages(services, session_id, [32 * 1024])
    monkeypatch.setattr(stdio, "MAX_FRAME_BYTES", 16 * 1024)
    stdin = io.StringIO(
        _req("big", "session.history", {"ref": session_id})
        + "\n"
        + _req("next", "session.history", {"ref": empty})
        + "\n"
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    assert stdio.run_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    big = next(line for line in lines if line.get("id") == "big")
    assert big["ok"] is False
    assert big["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert big["error"]["details"]["method"] == "session.history"
    # The connection survives: the following request is answered normally.
    assert next(line for line in lines if line.get("id") == "next")["ok"] is True
    assert "session.history" in stderr.getvalue()
    assert all(len(line) <= 16 * 1024 for line in stdout.getvalue().splitlines())


def test_oversized_event_is_shortened_or_dropped(monkeypatch) -> None:
    monkeypatch.setattr(stdio, "MAX_FRAME_BYTES", 64 * 1024)
    monkeypatch.setattr(frames, "_STRING_CAPS", (8 * 1024,))
    out, err = io.StringIO(), io.StringIO()
    event = {"event": "tool.completed", "payload": {"result": "r" * (200 * 1024)}}
    assert stdio._emit(out, event, err) is True
    sent = json.loads(out.getvalue())
    assert sent["event"] == "tool.completed"
    assert "characters omitted" in sent["payload"]["result"]
    out, err = io.StringIO(), io.StringIO()
    many = {"event": "tool.completed", "payload": {"rows": ["s" * 100] * 2000}}
    assert stdio._emit(out, many, err) is True
    assert out.getvalue() == ""
    assert "dropped" in err.getvalue()
