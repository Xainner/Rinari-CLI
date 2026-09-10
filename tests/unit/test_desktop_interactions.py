import json
import threading
import time
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.turns import _ActiveTurn
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError


@pytest.fixture
def desktop(app_ctx, tmp_path):
    services = build_services(app_ctx, user_home=tmp_path)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="test-secret",
        )
    )
    services.models.add("fake", "test-model", "test-model")
    services.providers.use("fake")
    server = EngineServer(services, user_home=tmp_path)
    yield server
    server.close()


def create(desktop):
    return desktop._session_create({"chat": True})["session"]


def test_general_chats_have_independent_workspaces(desktop):
    a, b = create(desktop), create(desktop)
    assert a["kind"] == b["kind"] == "CHAT"
    assert a["project_id"] is None
    assert a["current_cwd"] != b["current_cwd"]
    assert Path(a["current_cwd"]).is_relative_to(desktop._services.ctx.home / "workspaces")
    assert Path(a["current_cwd"]).is_dir()


def test_known_legacy_title_repaired_without_altering_unicode_titles(desktop):
    session = create(desktop)
    services = desktop._services
    title = "Nueva conversación"
    services.sessions.rename(session["id"], title.encode("utf-8").decode("cp1252"))
    services.sessions.list()
    assert services.sessions.show(session["id"]).title == title
    services.sessions.rename(session["id"], "Diseño de aplicación — 日本語")
    services.sessions.list()
    assert services.sessions.show(session["id"]).title == "Diseño de aplicación — 日本語"


def test_move_preserves_origin_and_reads_historical_turn(desktop, tmp_path):
    session = create(desktop)
    origin = Path(session["current_cwd"])
    (origin / "plan with spaces.md").write_text("# Original", encoding="utf-8")
    turn = _ActiveTurn("turn-origin", session["id"], None, threading.Event())
    desktop._turns._activity_cb(turn)("turn.started", {})
    turn.done.set()
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "plan with spaces.md").write_text("# Other project", encoding="utf-8")
    project = desktop._services.projects.upsert(destination)
    moved = desktop._desktop.move({"session_id": session["id"], "project_id": project.id})[
        "session"
    ]
    assert moved["id"] == session["id"]
    assert moved["current_cwd"] == str(destination.resolve())
    assert moved["kind"] == "PROJECT"
    old = desktop._desktop.read(
        {"session_id": session["id"], "turn_id": turn.turn_id, "path": "plan with spaces.md"}
    )
    assert old["content"] == "# Original"
    new = desktop._desktop.read({"session_id": session["id"], "path": "plan with spaces.md"})
    assert new["content"] == "# Other project"
    returned = desktop._desktop.move({"session_id": session["id"], "project_id": None})["session"]
    assert returned["kind"] == "CHAT" and returned["project_id"] is None
    assert (origin / "plan with spaces.md").exists()


def test_move_rejects_active_turn_and_queue(desktop, tmp_path):
    session = create(desktop)
    turn = _ActiveTurn("busy", session["id"], None, threading.Event())
    desktop._turns._turns[turn.turn_id] = turn
    with pytest.raises(EngineProtocolError, match="Wait for"):
        desktop._desktop.move({"session_id": session["id"]})
    turn.done.set()
    desktop._turns.queue_add(session["id"], "later")
    with pytest.raises(EngineProtocolError, match="queue"):
        desktop._desktop.move({"session_id": session["id"]})
    desktop._turns.queue_clear(session["id"])


def test_move_respects_cli_lock_and_preserves_scoped_baselines(desktop, tmp_path):
    from rinari.sessions.turn_lock import SessionTurnLock
    from rinari.shared.errors import ConflictError
    from rinari.storage.records import WorktreeBaselineRecord

    session = create(desktop)
    ctx = desktop._services.ctx
    destination = tmp_path / "project"
    destination.mkdir()
    project = desktop._services.projects.upsert(destination)
    ctx.worktree_repo.insert_many(
        session["id"],
        [
            WorktreeBaselineRecord(
                session_id=session["id"],
                path="old.md",
                git_status=" M",
                created_at="2026-09-09",
            )
        ],
    )
    ctx.pin_repo.pin(session["id"], "file", "old.md", "Old source", "2026-09-09")
    lock_path = ctx.layout.dir("sessions") / f"{session['id']}.turn.lock"
    with SessionTurnLock(lock_path, session["id"]), pytest.raises(ConflictError):
        desktop._desktop.move({"session_id": session["id"], "project_id": project.id})
    desktop._desktop.move({"session_id": session["id"], "project_id": project.id})
    assert ctx.worktree_repo.list(session["id"]) == []
    assert ctx.pin_repo.list(session["id"]) == []
    moved_event = next(e for e in ctx.event_repo.list(session["id"]) if e.type == "session.moved")
    assert moved_event.payload["baseline"][0]["path"] == "old.md"
    returned = desktop._desktop.move({"session_id": session["id"]})["session"]
    assert returned["current_cwd"] == session["current_cwd"]
    assert ctx.worktree_repo.list(session["id"])[0].path == "old.md"


@pytest.mark.parametrize("case", ["missing", "large", "binary", "traversal", "unknown-turn"])
def test_preview_failures(desktop, case):
    session = create(desktop)
    root = Path(session["current_cwd"])
    params = {"session_id": session["id"], "path": "file.md"}
    if case == "large":
        (root / "file.md").write_bytes(b"a" * (512 * 1024 + 1))
    if case == "binary":
        (root / "file.md").write_bytes(b"\0binary")
    if case == "traversal":
        params["path"] = "../private.md"
    if case == "unknown-turn":
        params["turn_id"] = "unknown"
    with pytest.raises(EngineProtocolError):
        desktop._desktop.read(params)


def question_wait(desktop):
    session = create(desktop)
    turn = _ActiveTurn("question-turn", session["id"], None, threading.Event())
    desktop._turns._turns[turn.turn_id] = turn
    token = CancellationToken()
    result = {}

    def worker():
        try:
            result["value"] = desktop._turns.questions.ask(
                turn,
                desktop._turns._activity_cb(turn),
                {
                    "questions": [
                        {
                            "id": "choice",
                            "title": "Next?",
                            "options": [{"label": "Plan", "recommended": True}],
                        }
                    ]
                },
                token,
                time.time() + 5,
            )
        except CancelledError:
            result["cancelled"] = True

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        questions = desktop._turns.questions.list(session["id"])["questions"]
        if questions:
            return session, turn, token, thread, result, questions[0]
        time.sleep(0.01)
    token.cancel()
    thread.join(2)
    pytest.fail("No question emitted")


@pytest.mark.parametrize(
    "status,answers",
    [("answered", {"choice": "Plan"}), ("answered", {"choice": "My own answer"}), ("skipped", {})],
)
def test_question_round_trip_and_durable_history(desktop, status, answers):
    session, turn, token, thread, result, request = question_wait(desktop)
    try:
        assert thread.is_alive()  # Recommended choice never answers automatically.
        snapshot = desktop._snapshot_get({})["snapshot"]
        assert snapshot["pending_questions"][0]["request_id"] == request["request_id"]
        params = {
            "session_id": session["id"],
            "request_id": request["request_id"],
            "status": status,
            "answers": answers,
        }
        desktop._turns.questions.resolve(params)
        thread.join(2)
        assert result["value"]["answers"] == answers
        with pytest.raises(EngineProtocolError):
            desktop._turns.questions.resolve(params)
        assert desktop._turns.questions.list(session["id"])["questions"][0]["status"] == status
        # New broker reconstructs answered state from shared persistent events.
        from rinari.engine_protocol.questions import QuestionBroker

        assert (
            QuestionBroker(desktop._services, lambda _: False).list(session["id"])["questions"][0][
                "answers"
            ]
            == answers
        )
    finally:
        token.cancel()
        thread.join(2)
        turn.done.set()


def test_question_cancellation_and_invalid_answers(desktop):
    session, turn, token, thread, result, request = question_wait(desktop)
    try:
        with pytest.raises(EngineProtocolError):
            desktop._turns.questions.resolve(
                {"session_id": session["id"], "request_id": request["request_id"], "answers": {}}
            )
        token.cancel()
        thread.join(2)
        assert result["cancelled"]
        assert desktop._turns.questions.list(session["id"])["questions"][0]["status"] == "expired"
    finally:
        token.cancel()
        thread.join(2)
        turn.done.set()


def test_plan_tool_asks_and_resumes_real_agent_loop(desktop, monkeypatch):
    from rinari.cli import agent_runtime
    from rinari.models.types import ModelResponse, ProviderCapabilities, StopReason, ToolCall

    class Model:
        def __init__(self):
            self.requests = []

        def capabilities(self):
            return ProviderCapabilities(streaming=False, tool_calls=True)

        def invoke(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id="ask-1",
                            name="user.ask",
                            arguments={
                                "questions": [{"id": "scope", "title": "What scope?"}],
                            },
                        ),
                    ),
                    stop_reason=StopReason.TOOL_CALLS,
                )
            return ModelResponse(
                content="Plan based on your answer", stop_reason=StopReason.END_TURN
            )

    model = Model()
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *args: model)
    session = desktop._session_create({"chat": True, "mode": "plan"})["session"]
    response = desktop.handle_line(
        json.dumps(
            {
                "id": "start",
                "method": "session.turn.start",
                "params": {"session_id": session["id"], "message": "Plan it"},
            }
        )
    )
    assert response["ok"], response
    deadline = time.monotonic() + 8
    seen = []
    try:
        while time.monotonic() < deadline:
            events = desktop._turns.drain_events()
            seen.extend(events)
            for event in events:
                if event["event"] == "question.requested":
                    desktop._turns.questions.resolve(
                        {
                            "session_id": session["id"],
                            "request_id": event["payload"]["request_id"],
                            "answers": {"scope": "Small scope"},
                        }
                    )
            if any(e["event"] in ("turn.completed", "turn.failed", "turn.cancelled") for e in seen):
                break
            time.sleep(0.02)
        assert any(e["event"] == "question.requested" for e in seen), json.dumps(seen, indent=2)
        assert any(e["event"] == "turn.completed" for e in seen), json.dumps(seen, indent=2)
        assert "Small scope" in str(model.requests[-1])
        assert next(e for e in seen if e["event"] == "turn.started")["payload"]["mode"] == "plan"
        history = desktop._session_timeline({"ref": session["id"]})
        assert history["turns"][-1]["mode"] == "plan"
    finally:
        if desktop._turns.has_active_turn(session["id"]):
            desktop._turns.cancel_turn(session["id"])
