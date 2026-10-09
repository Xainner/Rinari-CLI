"""Visible, controllable memory: learned facts, cards, settings and recall."""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.memory.service import MemoryService
from rinari.storage.records import SessionEventRecord, SessionMessageRecord, SessionRecord
from rinari.tools.definition import ToolErrorCode, TurnState
from rinari.tools.native.memory import memory_propose, memory_remember
from tests.unit.test_tool_runtime import _ctx

SESSION = "ses_learn"


def _session(app_ctx, session_id: str = SESSION, root: Path | None = None) -> str:
    now = "2026-01-01T00:00:00Z"
    app_ctx.session_repo.insert(
        SessionRecord(
            id=session_id,
            kind="PROJECT" if root else "CHAT",
            title="t",
            project_id=None,
            project_root_snapshot=str(root) if root else None,
            created_cwd=str(root or "."),
            current_cwd=str(root or "."),
            provider_id="",
            model_id="",
            profile_id="",
            mode="default",
            state="active",
            compact_state=None,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )
    return session_id


def _tool_ctx(app_ctx, tmp_path, *, project: bool = False, events: list | None = None):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    memory = MemoryService(app_ctx)
    _session(app_ctx, root=root if project else None)
    sink = (lambda name, payload: events.append((name, payload))) if events is not None else None
    ctx = replace(
        _ctx(tmp_path, root, kind="PROJECT" if project else "CHAT"),
        session_id=SESSION,
        memory=memory,
        activity_sink=sink,
    )
    return memory, ctx, root


def _propose(ctx, **kwargs):
    return memory_propose({"topic": "saturno", "text": "Saturno is at 10.0.0.7", **kwargs}, ctx)


# -- A. setting ------------------------------------------------------------------


def test_learned_facts_setting_defaults_to_ask_and_persists(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    assert memory.learned_facts_mode() == "ask"
    assert memory.set_learned_facts_mode("auto") == "auto"
    assert MemoryService(app_ctx).learned_facts_mode() == "auto"
    with pytest.raises(Exception, match="ask or auto"):
        memory.set_learned_facts_mode("always")


# -- B/C. memory.propose -------------------------------------------------------------


def test_ask_mode_creates_a_pending_card_and_stores_nothing(app_ctx, tmp_path) -> None:
    events: list = []
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path, events=events)
    result = _propose(ctx, kind="environment")
    assert result.ok, result.error
    assert result.data["status"] == "pending"
    assert "do not wait" in result.data["message"]
    assert memory.list_user() == []
    [(name, payload)] = events
    assert name == "memory.candidate.created"
    assert payload == {
        "candidate_id": result.data["id"],
        "topic": "saturno",
        "text": "Saturno is at 10.0.0.7",
        "kind": "environment",
        "scope": "user",
        "reason": "learned facts wait for the owner's approval",
        "sensitive": False,
    }
    [candidate] = memory.list_candidates()
    assert candidate["classification"] == "learned"
    # Not tied to an owner message: the end-of-turn capture stays untouched.
    assert candidate["message_id"] == ""


def test_auto_mode_saves_with_learned_provenance_and_emits_remembered(app_ctx, tmp_path) -> None:
    events: list = []
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path, events=events)
    memory.set_learned_facts_mode("auto")
    result = _propose(ctx, kind="environment")
    assert result.data["status"] == "saved"
    [row] = memory.list_user()
    assert row["provenance"] == f"learned:session/{SESSION}"
    assert row["kind"] == "environment" and row["scope"] == "user"
    assert events == [
        (
            "memory.remembered",
            {
                "memory_id": row["id"],
                "topic": "saturno",
                "text": "Saturno is at 10.0.0.7",
                "kind": "environment",
                "scope": "user",
            },
        )
    ]


def test_sensitive_learned_fact_waits_even_in_auto(app_ctx, tmp_path) -> None:
    events: list = []
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path, events=events)
    memory.set_learned_facts_mode("auto")
    result = memory_propose(
        {"topic": "salud", "text": "The owner takes insulin before lunch", "kind": "fact"}, ctx
    )
    assert result.data["status"] == "pending"
    assert events[0][1]["sensitive"] is True
    assert memory.list_candidates()[0]["classification"] == "learned_sensitive"
    # An IP "dirección" is infrastructure, not personal data.
    assert not memory.learned_needs_consent("servidor", "La dirección IP del servidor es 10.0.0.7")
    assert memory.learned_needs_consent("casa", "La dirección de su casa es Calle 5")


def test_untrusted_turns_propose_instead_of_saving(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    memory.set_learned_facts_mode("auto")
    state = TurnState()
    state.mark_external("web.fetch")
    external = _propose(replace(ctx, turn_state=state))
    assert external.data["status"] == "pending"
    peer = memory_propose(
        {"topic": "otro", "text": "Port 8080 serves the API"}, replace(ctx, origin_kind="peer")
    )
    assert peer.data["status"] == "pending"
    reasons = {row["reason"] for row in memory.list_candidates()}
    assert any("outside this machine" in reason for reason in reasons)
    assert any("did not start" in reason for reason in reasons)


def test_secrets_are_refused_including_redactor_shapes(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    memory.set_learned_facts_mode("auto")
    # Built at runtime: secret-looking literals do not belong in the repository.
    password = "hunter" + "2" + "Xq9z"
    for text in (
        "mysql -u root -p" + password + " app",
        "postgres://app:" + password + "@db.local:5432/app",
        "deploy uses --token " + password + "abc",
    ):
        result = _propose(ctx, text=text, kind="workflow")
        assert not result.ok, text
        assert result.error.code is ToolErrorCode.VALIDATION_FAILED
        assert "Do not retry" in result.error.message
    assert memory.list_user() == [] and memory.list_candidates() == []


def test_duplicates_declined_and_forgotten_facts_are_not_stored_again(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    pending = _propose(ctx)
    assert _propose(ctx).data["status"] == "already_proposed"
    memory.resolve_candidate(pending.data["id"], "deny")
    assert _propose(ctx, text="Saturno is at 10.0.0.7.").data["status"] == "declined"

    memory.set_learned_facts_mode("auto")
    saved = _propose(ctx, topic="dev server", text="The dev server runs with npm run dev on 5173")
    assert saved.data["status"] == "saved"
    again = _propose(ctx, topic="otro", text="the dev server runs with  npm run dev on 5173")
    assert again.data["status"] == "already_known" and again.data["id"] == saved.data["id"]
    row = memory.get_user(saved.data["id"])
    memory.forget_user(row["id"], expected_revision=row["revision"])
    forgotten = _propose(
        ctx, topic="dev server", text="The dev server runs with npm run dev on 5173"
    )
    assert forgotten.data["status"] == "forgotten"
    assert memory.list_user() == []


def test_project_scope_needs_a_project(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    memory.set_learned_facts_mode("auto")
    result = _propose(ctx, scope="project", kind="workflow")
    assert result.data["scope"] == "user"
    assert "no project" in result.notes[0]


def test_project_learned_fact_goes_to_project_memory(app_ctx, tmp_path) -> None:
    events: list = []
    memory, ctx, root = _tool_ctx(app_ctx, tmp_path, project=True, events=events)
    memory.set_learned_facts_mode("auto")
    result = memory_propose(
        {
            "topic": "tests",
            "text": "Tests run with uv run pytest -q",
            "kind": "workflow",
            "scope": "project",
        },
        ctx,
    )
    assert result.data["status"] == "saved" and result.data["scope"] == "project"
    [row] = memory.list_project(str(root.resolve()))
    assert row["scope"] == "project" and row["kind"] == "workflow"
    assert events[0][1]["scope"] == "project"


def test_a_subagent_cannot_propose(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    result = _propose(replace(ctx, session_id=f"{SESSION}-agent-1"))
    assert result.error.code is ToolErrorCode.PERMISSION_DENIED
    assert memory.list_candidates() == []


def test_owner_remember_pending_also_announces_a_card(app_ctx, tmp_path) -> None:
    events: list = []
    _memory, ctx, _ = _tool_ctx(app_ctx, tmp_path, events=events)
    app_ctx.message_repo.append_many(
        SESSION,
        [SessionMessageRecord(id="m1", session_id=SESSION, seq=0, role="user", content="x")],
    )
    ctx = replace(ctx, memory_source={"session_id": SESSION, "message_id": "m1", "text": "x"})
    result = memory_remember(
        {"scope": "user", "topic": "salud", "text": "Toma insulina por la mañana."}, ctx
    )
    assert result.data["pending"] is True
    assert events[0][0] == "memory.candidate.created" and events[0][1]["sensitive"] is True
    plain = memory_remember({"scope": "user", "topic": "idioma", "text": "Habla español."}, ctx)
    assert plain.ok
    assert events[-1][0] == "memory.remembered"


# -- D. candidates ------------------------------------------------------------------


def test_resolve_saves_the_edited_version_and_rechecks_secrets(app_ctx, tmp_path) -> None:
    memory, ctx, root = _tool_ctx(app_ctx, tmp_path, project=True)
    first = _propose(ctx, kind="environment")
    with pytest.raises(Exception, match="credential"):
        memory.resolve_candidate(
            first.data["id"], "allow_once", text="ssh password: " + "hunter" + "2Xq9zzz"
        )
    resolved = memory.resolve_candidate(
        first.data["id"], "allow_once", text="Saturno (Ubuntu) is at 10.0.0.8", topic="saturno host"
    )
    assert resolved["status"] == "accepted" and resolved["scope"] == "user"
    stored = memory.get_user(resolved["memory_id"])
    assert stored["text"] == "Saturno (Ubuntu) is at 10.0.0.8"
    assert stored["provenance"] == f"learned:session/{SESSION}"

    project = memory_propose(
        {
            "topic": "build",
            "text": "Build with npm run build",
            "kind": "workflow",
            "scope": "project",
        },
        ctx,
    )
    accepted = memory.resolve_candidate(project.data["id"], "allow_once")
    assert memory.list_project(str(root.resolve()))[0]["id"] == accepted["memory_id"]
    with pytest.raises(Exception, match="only apply when approving"):
        memory.resolve_candidate(project.data["id"], "deny", text="x")


def test_candidates_list_filters_and_shape(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    a = _propose(ctx)
    _propose(ctx, topic="b", text="Rinari runs on port 7777")
    memory.resolve_candidate(a.data["id"], "deny")
    assert [row["status"] for row in memory.list_candidates()] == ["pending"]
    assert [row["status"] for row in memory.list_candidates(status="resolved")] == ["denied"]
    rows = memory.list_candidates(status="all")
    assert len(rows) == 2
    assert {"id", "session_id", "topic", "text", "kind", "scope", "reason", "sensitive"} <= set(
        rows[0]
    )


# -- E/G. records and recall -----------------------------------------------------------


def test_search_ranks_by_terms_not_the_whole_query(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    memory.remember_user("Saturno corre Ubuntu con una GPU NVIDIA", topic="saturno hardware")
    memory.remember_user("Casa3090 aloja el bot de Discord", topic="casa3090")
    memory.remember_user("Configuración del servidor Saturno en /srv", topic="saturno config")
    rows = memory.search_user("¿qué GPU tiene saturno?")
    assert [row["topic"] for row in rows][:2] == ["saturno hardware", "saturno config"]
    assert all(row["scope"] == "user" for row in rows)
    # Accents fold: "configuracion" finds "Configuración".
    assert memory.search_user("configuracion")[0]["topic"] == "saturno config"
    assert memory.search_user("kubernetes") == []


def test_episodic_recall_uses_the_query_without_a_project(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    memory.record_episodic("s1", "", "Fixed the Discord bot reconnect loop on casa3090")
    for index in range(12):
        memory.record_episodic("s1", "/repo", f"Unrelated refactor number {index}")
    rows = memory.search_episodic("", "discord bot")
    assert rows and rows[0]["summary"].startswith("Fixed the Discord bot")
    # An empty query still lists the newest first.
    assert memory.search_episodic("", "", limit=3)[0]["summary"] == "Unrelated refactor number 11"
    assert memory.search_episodic("/repo", "discord") == []


def test_project_records_have_revision_and_scope(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    stored = memory.remember_project("/repo", "Run with make dev", topic="dev", kind="workflow")
    row = memory.get_any(stored["id"])
    assert row["scope"] == "project" and row["revision"] == 1
    updated = memory.update_project(
        "/repo", row["id"], text="Run with make dev-all", expected_revision=1
    )
    assert updated["revision"] == 2
    with pytest.raises(Exception, match="revision conflict"):
        memory.forget_project("/repo", row["id"], expected_revision=1)
    assert memory.forget_project("/repo", row["id"], expected_revision=2)


# -- F. prompt ----------------------------------------------------------------------------


def test_environment_facts_reach_the_prompt_as_a_bounded_list(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    memory.remember_user(
        "Saturno is at 10.0.0.7 (ssh as deploy)", topic="saturno", kind="environment"
    )
    memory.remember_project("/repo", "Start with npm run dev", topic="dev", kind="workflow")
    memory.remember_user("Prefers short answers", topic="style")
    segment = memory.prompt_segment("/repo", query="write a poem about cats")
    assert "Known environment and workflows:" in segment
    assert "- saturno: Saturno is at 10.0.0.7 (ssh as deploy)" in segment
    assert "- [project] dev: Start with npm run dev" in segment
    assert "Other records:" in segment and "[preference] Prefers short answers" in segment
    # Other projects' workflows stay out.
    assert "npm run dev" not in (memory.prompt_segment("/other") or "")

    for index in range(40):
        memory.remember_user(
            f"host-{index} is at 10.1.0.{index} " + "x" * 400, topic=f"h{index}", kind="environment"
        )
    segment = memory.prompt_segment(None)
    block = segment.split("Known environment and workflows:\n", 1)[1].split("\nOther records:", 1)[
        0
    ]
    assert len(block) <= 2500
    assert block.count("\n- ") < 20


# -- H. tombstones ----------------------------------------------------------------------------


def test_deleting_a_conversation_tombstones_only_owner_messages(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    app_ctx.message_repo.append_many(
        "gone",
        [
            SessionMessageRecord(id="u1", session_id="gone", seq=0, role="user", content="hola"),
            SessionMessageRecord(id="a1", session_id="gone", seq=1, role="assistant", content="hi"),
            SessionMessageRecord(id="t1", session_id="gone", seq=2, role="tool", content="{}"),
        ],
    )
    memory.delete_conversation("gone")
    assert memory.repo.suppressed_message_ids("gone") == {"u1"}
    assert memory.conversation_control("gone")["mode"] == "deleted"


def test_migration_drops_tombstones_of_non_owner_and_gone_messages(app_ctx) -> None:
    db = app_ctx.db
    _session(app_ctx, "alive")
    app_ctx.message_repo.append_many(
        "alive",
        [
            SessionMessageRecord(id="u1", session_id="alive", seq=0, role="user", content="a"),
            SessionMessageRecord(id="a1", session_id="alive", seq=1, role="assistant", content="b"),
        ],
    )
    for session_id, message_id in (
        ("alive", "u1"),
        ("alive", "a1"),
        ("gone", "x1"),
        ("gone", "x2"),
    ):
        db.execute(
            "INSERT INTO memory_source_suppressions(session_id, message_id, created_at) "
            "VALUES (?, ?, 't')",
            (session_id, message_id),
        )
    memory = MemoryService(app_ctx)
    memory.repo.set_control("gone", "deleted", "t")
    sql = (
        Path(__file__).parents[2]
        / "src/rinari/storage/migrations/0045_user_source_suppressions.sql"
    ).read_text(encoding="utf-8")
    for statement in (part.strip() for part in sql.split(";")):
        if statement:
            db.execute(statement)
    rows = db.query("SELECT session_id, message_id FROM memory_source_suppressions")
    assert {(row["session_id"], row["message_id"]) for row in rows} == {("alive", "u1")}
    assert memory.conversation_control("gone")["mode"] == "deleted"


# -- protocol ----------------------------------------------------------------------------------


@pytest.fixture
def server(app_ctx, tmp_path):
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
    return EngineServer(services)


_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"{method}-{next(_IDS)}", "method": method}
    if params is not None:
        line["params"] = params
    return server.handle_line(json.dumps(line))


def test_protocol_settings_roundtrip(server) -> None:
    assert _call(server, "memory.settings.get")["result"] == {"learned_facts": "ask"}
    assert _call(server, "memory.settings.set", {"learned_facts": "auto"})["result"] == {
        "learned_facts": "auto"
    }
    assert _call(server, "memory.settings.get")["result"] == {"learned_facts": "auto"}
    bad = _call(server, "memory.settings.set", {"learned_facts": "sometimes"})
    assert bad["error"]["code"] == "INVALID_PARAMS"
    info = _call(server, "engine.info")
    assert info["result"]["capabilities"]["learned_memory_v1"] is True


def test_protocol_resolves_a_learned_card_during_a_turn_and_records_it(
    server, app_ctx, monkeypatch
) -> None:
    memory = server._services.memory
    _session(app_ctx)
    proposed = memory.propose_learned(SESSION, text="Saturno is at 10.0.0.7", topic="saturno")
    candidate_id = proposed["candidate"]["id"]
    app_ctx.event_repo.insert(
        SessionEventRecord(
            id="evt-card",
            session_id=SESSION,
            seq=app_ctx.event_repo.next_seq(SESSION),
            type="memory.candidate.created",
            payload={"candidate_id": candidate_id, "turn_id": "turn-1"},
            created_at="2026-01-01T00:00:00Z",
            turn_id="turn-1",
            activity_seq=3,
        )
    )
    monkeypatch.setattr(server._turns, "has_active_turns", lambda: True)
    resolved = _call(
        server,
        "memory.candidate.resolve",
        {"id": candidate_id, "decision": "allow_once", "topic": "saturno host"},
    )
    assert resolved["ok"], resolved
    row = resolved["result"]["candidate"]
    assert row["status"] == "accepted" and row["memory_id"]
    [event] = [e for e in app_ctx.event_repo.list(SESSION) if e.type == "memory.candidate.resolved"]
    assert event.turn_id == "turn-1"
    assert event.payload["status"] == "approved"
    assert event.payload["memory_id"] == row["memory_id"]

    listed = _call(server, "memory.candidates.list", {"status": "resolved"})
    assert listed["result"]["count"] == 1
    assert listed["result"]["candidates"][0]["scope"] == "user"

    # A learned record can be undone from its card while the turn runs.
    record = memory.get_user(row["memory_id"])
    forgotten = _call(
        server, "memory.forget", {"id": record["id"], "expected_revision": record["revision"]}
    )
    assert forgotten["result"]["forgotten"] is True
    # Other personal memory still waits for the turn to end.
    other = memory.remember_user("Prefers tea", topic="drinks", provenance="panel")
    blocked = _call(server, "memory.forget", {"id": other["id"], "expected_revision": 1})
    assert blocked["error"]["code"] == "TURN_RUNNING"


def test_protocol_lists_and_edits_project_records(server) -> None:
    memory = server._services.memory
    memory.remember_user("Prefers tea", topic="drinks")
    stored = memory.remember_project("/repo", "Start with make dev", topic="dev", kind="workflow")
    default = _call(server, "memory.list")
    assert [row["scope"] for row in default["result"]["records"]] == ["user"]
    everything = _call(server, "memory.list", {"scope": "all"})
    assert {row["scope"] for row in everything["result"]["records"]} == {"user", "project"}
    only = _call(server, "memory.list", {"scope": "all", "kind": "workflow"})
    assert [row["id"] for row in only["result"]["records"]] == [stored["id"]]
    record = only["result"]["records"][0]
    for key in (
        "id",
        "topic",
        "text",
        "kind",
        "scope",
        "provenance",
        "created_at",
        "updated_at",
        "revision",
    ):
        assert key in record
    found = _call(server, "memory.search", {"query": "make dev", "scope": "all"})
    assert found["result"]["records"][0]["id"] == stored["id"]

    updated = _call(
        server,
        "memory.update",
        {"id": stored["id"], "expected_revision": 1, "text": "make dev-all"},
    )
    assert updated["result"]["scope"] == "project"
    assert updated["result"]["record"]["revision"] == 2
    gone = _call(server, "memory.forget", {"id": stored["id"], "expected_revision": 2})
    assert gone["result"] == {"scope": "project", "id": stored["id"], "forgotten": True}


def test_card_events_share_one_timeline_entry(server) -> None:
    turns = server._turns
    created = turns._activity_key(None, "memory.candidate.created", {"candidate_id": "c1"})
    resolved = turns._activity_key(
        None, "memory.candidate.resolved", {"candidate_id": "c1", "status": "approved"}
    )
    assert created == resolved == "memory-candidate:c1"
    assert turns._activity_key(None, "memory.remembered", {"memory_id": "m1"}) == "memory:m1"
