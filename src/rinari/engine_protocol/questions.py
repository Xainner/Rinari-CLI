"""Cancellable question waits; durable history uses the engine event store."""

import threading
import time

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.shared.clock import now_iso
from rinari.shared.errors import CancelledError
from rinari.storage.records import SessionEventRecord


class QuestionBroker:
    def __init__(self, services, active):
        self.services = services
        self.active = active
        self.lock = threading.RLock()
        self.pending = {}

    def ask(self, turn, emit, arguments, token, deadline):
        questions = arguments["questions"]
        if len({q["id"] for q in questions}) != len(questions):
            raise EngineProtocolError(INVALID_PARAMS, "Question IDs must be unique.")
        request_id = self.services.ctx.ids.new("question")
        request = {
            "request_id": request_id,
            "session_id": turn.session_id,
            "turn_id": turn.turn_id,
            "questions": questions,
            "status": "pending",
        }
        wait = threading.Event()
        with self.lock:
            self.pending[request_id] = (request, wait, emit, turn)
            try:
                emit("question.requested", request.copy())
            except Exception:
                self.pending.pop(request_id, None)
                raise
        try:
            while not wait.wait(0.05):
                if token is not None:
                    token.throw_if_cancelled()
                if turn.cancel_requested.is_set():
                    raise CancelledError("Question cancelled")
                if deadline is not None and time.time() >= deadline:
                    raise CancelledError("Question wait expired")
            return {
                "request_id": request_id,
                "status": request["status"],
                "answers": request.get("answers", {}),
            }
        finally:
            with self.lock:
                try:
                    if request["status"] == "pending":
                        request["status"] = "expired"
                        emit("question.expired", request.copy())
                finally:
                    self.pending.pop(request_id, None)

    def resolve(self, params):
        if not isinstance(params.get("request_id"), str):
            raise EngineProtocolError(INVALID_PARAMS, "request_id is required.")
        with self.lock:
            entry = self.pending.get(params.get("request_id"))
            if entry is None:
                raise EngineProtocolError("QUESTION_EXPIRED", "Question is no longer pending.")
            request, wait, emit, turn = entry
            if turn.cancel_requested.is_set() or turn.done.is_set():
                raise EngineProtocolError("QUESTION_EXPIRED", "Question turn was cancelled.")
            if request["status"] != "pending":
                raise EngineProtocolError("QUESTION_RESOLVED", "Question already answered.")
            if params.get("session_id") != request["session_id"]:
                raise EngineProtocolError(INVALID_PARAMS, "Question belongs to another session.")
            status = params.get("status", "answered")
            answers = params.get("answers", {})
            ids = {q["id"] for q in request["questions"]}
            if status not in ("answered", "skipped") or not isinstance(answers, dict):
                raise EngineProtocolError(INVALID_PARAMS, "Invalid answer.")
            if status == "answered" and (
                set(answers) != ids
                or any(
                    not isinstance(a, str) or not a.strip() or len(a) > 8000
                    for a in answers.values()
                )
            ):
                raise EngineProtocolError(
                    INVALID_PARAMS, "Answer every question or skip explicitly."
                )
            updated = {
                **request,
                "status": status,
                "answers": answers if status == "answered" else {},
            }
            emit("question.resolved", updated)
            request.update(updated)
            wait.set()
            return request.copy()

    def list(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(INVALID_PARAMS, "session_id is required.")
        self.services.sessions.show(session_id)
        requests = {}
        with self.lock:
            for row in self.services.ctx.event_repo.list(session_id):
                if row.type.startswith("question.") and row.payload.get("request_id"):
                    requests[row.payload["request_id"]] = dict(row.payload)
            for request in requests.values():
                if request.get("status") == "pending" and request["request_id"] not in self.pending:
                    request["status"] = "expired"
                    request["event"] = "question.expired"
                    ctx = self.services.ctx
                    with ctx.db.transaction():
                        ctx.event_repo.insert(
                            SessionEventRecord(
                                id=ctx.ids.new("evt"),
                                session_id=session_id,
                                seq=ctx.event_repo.next_seq(session_id),
                                type="question.expired",
                                payload=request.copy(),
                                created_at=now_iso(ctx.clock),
                                turn_id=request["turn_id"],
                                activity_seq=request.get("activity_seq"),
                            )
                        )
        return {"questions": list(requests.values())}
