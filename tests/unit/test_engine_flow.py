"""Flow projection (`flow.get`, capability `project_flow_v1`).

Stages are derived from persisted turn events, never estimated: a contiguous
run of turns in the same mode across the sessions of the scope is one stage,
a finished PLAN opens a cycle, progress only comes from the task graph, the
plan outcome or verification records, and unknown values are ``None``.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.flow import (
    MAX_STAGE_FILES,
    TurnFacts,
    build_flow,
    excerpt,
    group_stages,
    plan_heading,
    stage_progress,
    stage_status,
    turn_facts_from_events,
)
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason

# -- pure projection --------------------------------------------------------------


def _event(turn_id: str, type_: str, payload: dict | None = None, created_at: str = "t"):
    return SimpleNamespace(
        turn_id=turn_id, type=type_, payload=payload or {}, created_at=created_at
    )


def _turn(
    turn_id: str,
    mode: str,
    started: str,
    completed: str | None,
    status: str = "completed",
    message: str = "haz algo",
    *,
    session_id: str = "ses_a",
    extra: list | None = None,
):
    rows = [
        _event(
            turn_id,
            "turn.started",
            {"mode": mode, "message": message, "occurred_at": started},
            started,
        ),
        _event(turn_id, "model.started", {"model": "opus"}, started),
    ]
    rows.extend(extra or [])
    if completed is not None:
        rows.append(_event(turn_id, f"turn.{status}", {"occurred_at": completed}, completed))
    return rows


def _record(session_id: str = "ses_a", title: str = "Backend API"):
    return SimpleNamespace(id=session_id, title=title)


def test_turn_facts_fold_events_into_the_facts_a_flow_needs() -> None:
    events = _turn(
        "t1",
        "plan",
        "2026-09-17T10:00:00.000Z",
        "2026-09-17T10:01:00.000Z",
        message="Planifica la **migración**",
        extra=[
            _event(
                "t1",
                "model.content.completed",
                {"output_kind": "progress", "content": "# borrador"},
            ),
            _event(
                "t1",
                "model.content.completed",
                {"output_kind": "final", "content": "## Plan de migración\n\n1. paso"},
            ),
            _event("t1", "agent.started", {"agent": "explore"}),
            _event("t1", "agent.started", {"agent": "explore"}),
            _event(
                "t1",
                "turn.changes.completed",
                {"files": [{"path": "a.ts", "kind": "created"}, {"path": "b.ts"}]},
            ),
            _event("t1", "verification.completed", {"outcome": "done"}),
            _event("t1", "verification.completed", {"outcome": "failed"}),
            _event("t1", "approval.requested", {"approval_id": "apr_1"}),
        ],
    )
    # An event without turn_id is session-level noise for a flow.
    events.append(
        SimpleNamespace(turn_id=None, type="session.mode.changed", payload={}, created_at="x")
    )
    (facts,) = turn_facts_from_events(_record(), events)
    assert facts.mode == "plan" and facts.status == "completed"
    assert facts.started_at == "2026-09-17T10:00:00.000Z"
    assert facts.completed_at == "2026-09-17T10:01:00.000Z"
    assert facts.plan_heading == "Plan de migración"
    assert facts.models == {"opus": 1}
    assert facts.agents == {"explore": 2}
    assert [f["path"] for f in facts.files] == ["a.ts", "b.ts"]
    assert facts.files[1]["kind"] == "modified"
    assert (facts.verification_passed, facts.verification_failed) == (1, 1)
    # A finished turn is never "waiting on you", even with a dangling request.
    assert facts.pending_interventions == 0
    assert facts.origin_peer is False


def test_running_turn_counts_unresolved_interventions_and_peer_origin() -> None:
    events = [
        _event("t2", "turn.started", {"mode": "build", "message": "x", "origin": {"kind": "peer"}}),
        _event("t2", "approval.requested", {"approval_id": "apr_1"}),
        _event("t2", "approval.resolved", {"approval_id": "apr_1"}),
        _event("t2", "question.requested", {"request_id": "q_1"}),
    ]
    (facts,) = turn_facts_from_events(_record(), events)
    assert facts.status == "running"
    assert facts.pending_interventions == 1
    assert facts.origin_peer is True


def test_excerpt_and_plan_heading_strip_markdown_and_clip() -> None:
    assert excerpt("Hola **mundo** `x`\n\n```ts\ncode\n```\n- item") == "Hola mundo x item"
    assert excerpt("a" * 200).endswith("…") and len(excerpt("a" * 200)) == 160
    assert plan_heading("texto suelto\n# Título real ##\nmás") == "Título real"
    assert plan_heading("\n\n  primera línea sin encabezado\n") == "primera línea sin encabezado"
    assert plan_heading("") is None


def _facts(turn_id, mode, started, completed=None, status="completed", session="ses_a", **kw):
    return TurnFacts(
        turn_id=turn_id,
        session_id=session,
        session_title=session.upper(),
        mode=mode,
        status=status if completed else "running",
        started_at=started,
        completed_at=completed,
        user_message=kw.get("message", f"mensaje {turn_id}"),
        plan_heading=kw.get("heading"),
        files=kw.get("files", []),
        verification_passed=kw.get("passed", 0),
        verification_failed=kw.get("failed", 0),
        pending_interventions=kw.get("pending", 0),
        origin_peer=kw.get("peer", False),
    )


def test_stages_are_contiguous_mode_runs_across_sessions_with_plan_cycles() -> None:
    turns = [
        _facts("p1", "plan", "10:00", "10:05", heading="Diseño"),
        _facts("b1", "build", "10:10", "10:20"),
        _facts("b2", "build", "10:15", "10:30", session="ses_b"),  # interleaved, other session
        _facts("r1", "review", "10:40", "10:45"),
        _facts("p2", "plan", "11:00", "11:05", heading="Segunda fase"),
        _facts("b3", "build", "11:10", None),
    ]
    stages = group_stages(turns)
    assert [(s.kind, s.cycle_index, [t.turn_id for t in s.turns]) for s in stages] == [
        ("planning", 1, ["p1"]),
        ("implementation", 1, ["b1", "b2"]),
        ("review", 1, ["r1"]),
        ("planning", 2, ["p2"]),
        ("implementation", 2, ["b3"]),
    ]
    # A scope that starts in BUILD still has a cycle (1) without a PLAN.
    assert group_stages([_facts("b9", "build", "09:00", "09:01")])[0].cycle_index == 1
    assert group_stages([_facts("x1", None, "09:00", "09:01")])[0].kind == "implementation"


def test_stage_status_is_honest_about_running_failed_and_stopped_work() -> None:
    running = group_stages([_facts("b1", "build", "10:00", None)])[0]
    assert stage_status(running) == "active"
    waiting = group_stages([_facts("b1", "build", "10:00", None, pending=1)])[0]
    assert stage_status(waiting) == "needs_you"
    assert (
        stage_status(group_stages([_facts("b1", "build", "10:00", "10:01", status="failed")])[0])
        == "failed"
    )
    assert (
        stage_status(group_stages([_facts("b1", "build", "10:00", "10:01", status="cancelled")])[0])
        == "stopped"
    )
    assert (
        stage_status(group_stages([_facts("b1", "build", "10:00", "10:01", status="stopped")])[0])
        == "stopped"
    )
    # A failed turn followed by a completed one in the same stage: the stage ended well.
    recovered = group_stages(
        [
            _facts("b1", "build", "10:00", "10:01", status="failed"),
            _facts("b2", "build", "10:02", "10:03"),
        ]
    )[0]
    assert stage_status(recovered) == "done"


def test_progress_never_invents_a_number() -> None:
    open_plan = group_stages([_facts("p1", "plan", "10:00", None)])[0]
    assert stage_progress(open_plan, "active", None) is None
    done_plan = group_stages([_facts("p1", "plan", "10:00", "10:05")])[0]
    assert stage_progress(done_plan, "done", None) == 1.0

    build = group_stages([_facts("b1", "build", "10:10", "10:20")])[0]
    # Tasks touched during the stage drive the ratio; untouched ones do not count.
    tasks = [
        {"id": "1", "status": "done", "created_at": "10:11", "updated_at": "10:12"},
        {"id": "2", "status": "pending", "created_at": "10:11", "updated_at": "10:11"},
        {"id": "3", "status": "done", "created_at": "10:13", "updated_at": "10:14"},
        {"id": "old", "status": "pending", "created_at": "09:00", "updated_at": "09:00"},
    ]
    assert stage_progress(build, "done", tasks) == pytest.approx(2 / 3)
    # Without tasks: 1.0 only when the stage is done, otherwise unknown.
    assert stage_progress(build, "done", None) == 1.0
    running_build = group_stages([_facts("b1", "build", "10:10", None)])[0]
    assert stage_progress(running_build, "active", None) is None
    assert (
        stage_progress(
            running_build, "active", [{"id": "old", "status": "done", "created_at": "09:00"}]
        )
        is None
    )

    review = group_stages([_facts("r1", "review", "10:30", "10:40", passed=3, failed=1)])[0]
    assert stage_progress(review, "done", None) == 0.75
    quiet_review = group_stages([_facts("r1", "review", "10:30", "10:40")])[0]
    assert stage_progress(quiet_review, "done", None) is None


def test_build_flow_payload_summary_files_executors_and_anchor() -> None:
    files = [{"path": f"src/{i}.ts", "kind": "modified"} for i in range(MAX_STAGE_FILES + 3)]
    turns = [
        _facts(
            "p1",
            "plan",
            "2026-09-17T10:00:00.000Z",
            "2026-09-17T10:01:00.000Z",
            heading="Diseño API",
        ),
        _facts(
            "b1",
            "build",
            "2026-09-17T10:10:00.000Z",
            "2026-09-17T10:20:00.000Z",
            files=files,
            peer=True,
        ),
        _facts(
            "b2",
            "build",
            "2026-09-17T10:21:00.000Z",
            None,
            session="ses_b",
            files=[{"path": "src/0.ts", "kind": "deleted"}],
        ),
    ]
    turns[0].models.update({"opus": 2})
    turns[1].models.update({"opus": 1, "sonnet": 3})
    turns[1].agents.update({"explore": 1})
    flow = build_flow(
        scope={"kind": "project", "id": "proj", "title": "Demo", "root": "/repo"},
        turns=turns,
        tasks=[
            {"id": "1", "status": "done"},
            {"id": "2", "status": "in_progress"},
            {"id": "3", "status": "cancelled"},
        ],
        checkpoints=[
            {"created_at": "2026-09-17T10:15:00.000Z"},
            {"created_at": "2026-09-17T09:00:00.000Z"},
        ],
    )
    assert flow["scope"]["kind"] == "project"
    summary = flow["summary"]
    assert (
        summary["stages_total"] == 2
        and summary["stages_done"] == 1
        and summary["stages_active"] == 1
    )
    assert summary["turns_total"] == 3 and summary["turns_failed"] == 0
    assert summary["files_changed"] == MAX_STAGE_FILES + 3
    assert summary["tasks"] == {"total": 3, "done": 1, "open": 1}
    # Only the finished PLAN has a progress (1.0); the running BUILD has none.
    assert summary["progress"] == 1.0
    assert summary["started_at"] == "2026-09-17T10:00:00.000Z"
    assert summary["last_activity_at"] == "2026-09-17T10:21:00.000Z"

    plan, build = flow["stages"]
    assert plan["title"] == "Diseño API" and plan["kind"] == "planning" and plan["status"] == "done"
    assert plan["duration_ms"] == 60_000
    assert plan["executors"] == [{"model": "opus", "calls": 2}]
    assert plan["anchor"] == {"session_id": "ses_a", "turn_id": "p1"}
    assert build["id"] == "stg_b1" and build["status"] == "active" and build["completed_at"] is None
    assert build["duration_ms"] is None and build["progress"] is None
    assert build["title"] == "mensaje b1"
    assert build["executors"] == [{"model": "sonnet", "calls": 3}, {"model": "opus", "calls": 1}]
    assert build["agents"] == [{"agent": "explore", "runs": 1}]
    assert [s["session_id"] for s in build["sessions"]] == ["ses_a", "ses_b"]
    assert len(build["files"]) == MAX_STAGE_FILES and build["files_more"] == 3
    # The later turn's kind wins for a path touched twice, and it names its turn.
    assert build["files"][0] == {"path": "src/0.ts", "kind": "deleted", "turn_id": "b2"}
    assert build["checkpoints"] == 1 and build["origin_peer_turns"] == 1
    assert build["verification"] is None


def test_empty_scope_yields_an_empty_flow_not_an_error() -> None:
    flow = build_flow(
        scope={"kind": "session", "id": "s", "title": "s", "root": None}, turns=[], tasks=None
    )
    assert flow["stages"] == []
    assert flow["summary"] == {
        "stages_total": 0,
        "stages_done": 0,
        "stages_active": 0,
        "turns_total": 0,
        "turns_failed": 0,
        "files_changed": 0,
        "tasks": None,
        "progress": None,
        "started_at": None,
        "last_activity_at": None,
    }


# -- through the protocol ----------------------------------------------------------


class ScriptedModel:
    def __init__(self, scripted: list[ModelResponse] | None = None) -> None:
        self.scripted = list(scripted or [])
        self.requests: list[ModelRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.scripted:
            return self.scripted.pop(0)
        return ModelResponse(content="listo", stop_reason=StopReason.END_TURN)


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


@pytest.fixture
def models(monkeypatch):
    table: dict[str, ScriptedModel] = {}
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, record: table.setdefault(record.id, ScriptedModel()),
    )
    return table


_IDS = iter(range(1, 100_000))


def _call(server, method, params=None):
    line: dict = {"id": f"{method}-{next(_IDS)}", "method": method}
    if params is not None:
        line["params"] = params
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(server, method, params=None):
    response = _call(server, method, params)
    assert response["ok"] is True, response
    return response["result"]


def _wait_idle(server, session_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not server.turns.has_active_turn(session_id):
            return
        time.sleep(0.02)
    raise AssertionError(f"session {session_id} still busy")


def _plan(text: str) -> ModelResponse:
    return ModelResponse(content=text, stop_reason=StopReason.END_TURN)


def test_flow_get_for_a_project_spans_its_sessions_in_time_order(server, tmp_path, models):
    root = tmp_path / "project"
    root.mkdir()
    project = _ok(server, "project.add", {"path": str(root)})["project"]
    a = _ok(
        server, "session.create", {"project_id": project["id"], "mode": "plan", "title": "Backend"}
    )["session"]
    b = _ok(
        server, "session.create", {"project_id": project["id"], "mode": "build", "title": "Docs"}
    )["session"]
    models[a["id"]] = ScriptedModel([_plan("# Plan de la API\n\n1. Rutas\n2. Tests")])
    _ok(server, "session.turn.start", {"session_id": a["id"], "message": "planifica la API"})
    _wait_idle(server, a["id"])
    _ok(server, "session.turn.start", {"session_id": b["id"], "message": "implementa las rutas"})
    _wait_idle(server, b["id"])
    _ok(server, "session.mode.set", {"ref": a["id"], "mode": "review"})
    _ok(server, "session.turn.start", {"session_id": a["id"], "message": "revisa lo hecho"})
    _wait_idle(server, a["id"])

    flow = _ok(server, "flow.get", {"project_id": project["id"]})
    assert flow["scope"] == {
        "kind": "project",
        "id": project["id"],
        "title": "project",
        "root": project["canonical_root"],
    }
    kinds = [(stage["kind"], stage["cycle_index"], stage["status"]) for stage in flow["stages"]]
    assert kinds == [("planning", 1, "done"), ("implementation", 1, "done"), ("review", 1, "done")]
    plan, build, review = flow["stages"]
    assert plan["title"] == "Plan de la API" and plan["progress"] == 1.0
    assert plan["anchor"]["session_id"] == a["id"]
    assert build["sessions"] == [{"session_id": b["id"], "title": "Docs", "turns": 1}]
    assert build["excerpt"] == "implementa las rutas"
    # Executors come from the recorded model calls, not from the session's current model.
    assert plan["executors"] and plan["executors"][0]["calls"] >= 1
    assert review["verification"] is None and review["progress"] is None
    assert flow["summary"]["turns_total"] == 3 and flow["summary"]["stages_done"] == 3
    assert flow["summary"]["tasks"] == {"total": 0, "done": 0, "open": 0}
    assert flow["summary"]["started_at"] == plan["started_at"]

    # A lone session scope shows only that session's turns.
    single = _ok(server, "flow.get", {"session_id": b["id"]})
    assert single["scope"]["kind"] == "session" and single["scope"]["id"] == b["id"]
    assert [stage["kind"] for stage in single["stages"]] == ["implementation"]


def test_flow_get_validates_scope_and_reports_missing_targets(server):
    invalid = _call(server, "flow.get", {})
    assert invalid["ok"] is False and invalid["error"]["code"] == "INVALID_PARAMS"
    both = _call(server, "flow.get", {"project_id": "p", "session_id": "s"})
    assert both["ok"] is False and both["error"]["code"] == "INVALID_PARAMS"
    missing = _call(server, "flow.get", {"project_id": "proj_nope"})
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"
    missing_session = _call(server, "flow.get", {"session_id": "ses_nope"})
    assert missing_session["ok"] is False and missing_session["error"]["code"] == "NOT_FOUND"


def test_flow_get_degrades_when_the_project_folder_is_gone(server, tmp_path, models):
    root = tmp_path / "project"
    root.mkdir()
    project = _ok(server, "project.add", {"path": str(root)})["project"]
    a = _ok(server, "session.create", {"project_id": project["id"], "mode": "build"})["session"]
    _ok(server, "session.turn.start", {"session_id": a["id"], "message": "hola"})
    _wait_idle(server, a["id"])
    root.rename(tmp_path / "moved-away")
    flow = _ok(server, "flow.get", {"project_id": project["id"]})
    assert flow["summary"]["tasks"] is None
    assert [stage["status"] for stage in flow["stages"]] == ["done"]
    assert flow["stages"][0]["progress"] == 1.0


def test_flow_get_announces_its_capability(server):
    info = _ok(server, "engine.info")
    assert info["capabilities"]["project_flow_v1"] is True
