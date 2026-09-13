"""Independent adversarial coverage for selective memory and privacy controls."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.context.compact_state import CompactState
from rinari.memory.service import MemoryService
from rinari.models.types import ModelResponse, StopReason
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError
from rinari.storage.records import SessionMessageRecord
from rinari.tools.native.memory import memory_remember, memory_update
from tests.unit.test_conversation_persistence import FakeModel
from tests.unit.test_tool_runtime import _ctx


def _append(
    app_ctx,
    session_id: str,
    message_id: str,
    text: str,
    *,
    role: str = "user",
    turn_id: str | None = None,
) -> SessionMessageRecord:
    record = SessionMessageRecord(
        id=message_id,
        session_id=session_id,
        seq=0,
        role=role,
        content=text,
        turn_id=turn_id,
    )
    app_ctx.message_repo.append_many(session_id, [record])
    return app_ctx.message_repo.list(session_id)[-1]


def test_capture_requires_exact_owner_source_and_closed_benign_grammar(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    _append(app_ctx, "owner", "safe", "Prefiero respuestas breves")

    accepted = memory.capture_owner_message("owner", "safe", "Prefiero respuestas breves")
    assert accepted is not None
    assert accepted["status"] == "accepted"
    assert [row["text"] for row in memory.list_user()] == ["Prefiero respuestas breves"]
    source = memory.repo.sources_for_memory(accepted["memory_id"], live_only=True)[0]
    assert source["session_id"] == "owner" and source["message_id"] == "safe"
    assert source["quote"] == ""

    _append(app_ctx, "owner", "assistant", "Prefiero respuestas detalladas", role="assistant")
    assert (
        memory.capture_owner_message("owner", "assistant", "Prefiero respuestas detalladas")
        is None
    )
    _append(app_ctx, "owner", "mismatch", "Prefiero tono formal")
    assert memory.capture_owner_message("owner", "mismatch", "Prefiero tono casual") is None

    # A harmless-looking substring must not make a sensitive statement match
    # the safe allow-list as a wildcard.
    _append(app_ctx, "owner", "medical", "Prefiero usar mi insulina")
    pending = memory.capture_owner_message("owner", "medical", "Prefiero usar mi insulina")
    assert pending is not None
    assert pending["classification"] in ("review", "sensitive")
    assert pending["status"] == "pending"
    stored = dict(
        app_ctx.db.query_one("SELECT * FROM memory_candidates WHERE id = ?", (pending["id"],))
    )
    assert stored["text"] == ""
    assert len(memory.list_user()) == 1


def test_safe_preferences_keep_independent_dimensions_and_supersede_only_conflict(
    app_ctx,
) -> None:
    memory = MemoryService(app_ctx)
    statements = (
        ("length-short", "Prefiero respuestas breves"),
        ("language", "Prefiero que respondas en español"),
        ("length-long", "Prefiero respuestas detalladas"),
    )
    captured = []
    for message_id, text in statements:
        _append(app_ctx, "dimensions", message_id, text)
        captured.append(memory.capture_owner_message("dimensions", message_id, text))

    observed = [
        None if item is None else (item["status"], item["classification"], item["topic"])
        for item in captured
    ]
    assert all(
        item is not None and item["status"] == "accepted" for item in captured
    ), observed
    short, language, long = captured
    assert short["memory_id"] != language["memory_id"]
    assert long["memory_id"] != language["memory_id"]
    assert memory.get_user(short["memory_id"]) is None
    assert memory.get_user(language["memory_id"])["text"] == statements[1][1]
    assert memory.get_user(long["memory_id"])["text"] == statements[2][1]
    assert {row["text"] for row in memory.list_user()} == {
        statements[1][1],
        statements[2][1],
    }


def test_pending_sensitive_data_is_reference_only_and_secrets_never_commit(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    sensitive = "Recuerda que mi salario es 1000 unidades"
    _append(app_ctx, "sensitive", "salary", sensitive)
    candidate = memory.capture_owner_message("sensitive", "salary", sensitive)
    assert candidate is not None and candidate["status"] == "pending"

    raw = dict(
        app_ctx.db.query_one("SELECT * FROM memory_candidates WHERE id = ?", (candidate["id"],))
    )
    assert raw["text"] == ""
    assert sensitive not in repr(raw)
    assert memory.repo.sources_for_session("sensitive") == []
    # The authenticated owner panel can resolve the reference back to the
    # existing message without a second durable copy in the candidate table.
    assert memory.list_candidates()[0]["text"] == sensitive
    allowed = memory.resolve_candidate(candidate["id"], "allow_once")
    assert allowed["status"] == "accepted"
    source = memory.repo.sources_for_memory(allowed["memory_id"])[0]
    assert source["quote"] == ""

    secret = "Recuerda que mi token=abcdefghijklmnop1234567890"
    _append(app_ctx, "sensitive", "secret", secret)
    secret_candidate = memory.capture_owner_message("sensitive", "secret", secret)
    assert secret_candidate is not None and secret_candidate["status"] == "pending"
    with pytest.raises(InvalidUsageError):
        memory.resolve_candidate(secret_candidate["id"], "allow_once")
    assert all(secret not in row["text"] for row in memory.list_user())
    stored = dict(
        app_ctx.db.query_one(
            "SELECT * FROM memory_candidates WHERE id = ?", (secret_candidate["id"],)
        )
    )
    assert stored["text"] == ""


def test_exclusion_closes_automatic_deferred_and_tool_write_paths(app_ctx, tmp_path) -> None:
    memory = MemoryService(app_ctx)
    pending_text = "Prefiero usar mi insulina"
    _append(app_ctx, "excluded", "pending", pending_text)
    pending = memory.capture_owner_message("excluded", "pending", pending_text)
    assert pending is not None and pending["status"] == "pending"

    excluded = memory.exclude_conversation("excluded")
    assert excluded["mode"] == "excluded"
    assert memory.resolve_candidate(pending["id"], "allow_once")["status"] == "denied"
    visible, redacted = memory.redact_history(app_ctx.message_repo.list("excluded"))
    assert [record.id for record in visible] == ["pending"]
    assert redacted == 0

    _append(app_ctx, "excluded", "later", "Prefiero respuestas cortas")
    assert (
        memory.capture_owner_message("excluded", "later", "Prefiero respuestas cortas")
        is None
    )
    # A model tool call in that same session cannot bypass the durable control.
    project = tmp_path / "project"
    project.mkdir()
    tool_ctx = replace(_ctx(tmp_path, project), session_id="excluded", memory=memory)
    attempted = memory_remember(
        {"scope": "user", "topic": "formato", "text": "Prefiero respuestas cortas"},
        tool_ctx,
    )
    assert not attempted.ok
    assert memory.list_user() == []


def test_user_memory_tools_cannot_create_untracked_or_sensitive_records(
    app_ctx, tmp_path
) -> None:
    memory = MemoryService(app_ctx)
    project = tmp_path / "tool-project"
    project.mkdir()
    tool_ctx = replace(_ctx(tmp_path, project), session_id="tool-session", memory=memory)

    for text in (
        "Usa insulina antes del desayuno",
        "Tomo loratadina diariamente",
        "Prefiero respuestas breves",
    ):
        result = memory_remember(
            {"scope": "user", "topic": "preferencia", "text": text}, tool_ctx
        )
        assert not result.ok, text
    assert memory.list_user() == []

    panel = memory.remember_user(
        "Texto aprobado por panel", topic="panel", provenance="panel"
    )
    row = memory.get_user(panel["id"])
    update = memory_update(
        {
            "scope": "user",
            "id": panel["id"],
            "expected_revision": row["revision"],
            "text": "Tomo loratadina diariamente",
        },
        tool_ctx,
    )
    assert not update.ok
    assert memory.get_user(panel["id"])["text"] == "Texto aprobado por panel"


def test_source_revocation_preserves_independent_source_until_all_are_excluded(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    text = "Prefiero respuestas breves"
    _append(app_ctx, "one", "m1", text)
    _append(app_ctx, "two", "m2", text)
    first = memory.capture_owner_message("one", "m1", text)
    second = memory.capture_owner_message("two", "m2", text)
    assert first is not None and second is not None
    assert first["memory_id"] == second["memory_id"]

    memory.exclude_conversation("one")
    assert memory.get_user(first["memory_id"]) is not None
    assert [row["session_id"] for row in memory.repo.sources_for_memory(
        first["memory_id"], live_only=True
    )] == ["two"]

    memory.exclude_conversation("two")
    assert memory.get_user(first["memory_id"]) is None
    assert memory.prompt_segment(None) is None


def test_forget_and_delete_redact_source_turn_and_survive_restart(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("RINARI_KEYRING", "0")
    first_ctx = build_app_context(home=home, clock=FakeClock(start=1_700_000_000.0, step=1.0))
    memory = MemoryService(first_ctx)
    text = "Prefiero respuestas breves"
    _append(first_ctx, "forgotten", "owner", text, turn_id="turn-1")
    _append(
        first_ctx,
        "forgotten",
        "reply",
        "Lo recordaré",
        role="assistant",
        turn_id="turn-1",
    )
    _append(first_ctx, "forgotten", "keep", "Mensaje independiente", turn_id="turn-2")
    captured = memory.capture_owner_message("forgotten", "owner", text)
    assert captured is not None
    row = memory.get_user(captured["memory_id"])
    assert memory.forget_user(row["id"], expected_revision=row["revision"])

    visible, redacted = memory.redact_history(first_ctx.message_repo.list("forgotten"))
    assert redacted == 2
    assert [record.id for record in visible] == ["keep"]
    assert memory.capture_owner_message("forgotten", "owner", text) is None

    memory.record_episodic("forgotten", "", "Resumen derivado", provenance="session")
    deleted = memory.delete_conversation("forgotten")
    assert deleted["mode"] == "deleted"
    assert deleted["episodic_removed"] == 1
    assert memory.repo.sources_for_session("forgotten") == []
    first_ctx.close()

    reopened = build_app_context(home=home, clock=FakeClock(start=1_700_000_100.0, step=1.0))
    try:
        memory = MemoryService(reopened)
        assert memory.conversation_control("forgotten")["mode"] == "deleted"
        assert memory.repo.source_suppressed("forgotten", "owner")
        assert memory.repo.source_suppressed("forgotten", "reply")
        assert memory.repo.source_suppressed("forgotten", "keep")
        assert memory.capture_owner_message("forgotten", "owner", text) is None
        visible, redacted = memory.redact_history(reopened.message_repo.list("forgotten"))
        assert visible == [] and redacted == 3
        assert memory.search_episodic("", "Resumen") == []
    finally:
        reopened.close()


def test_ledger_import_purges_live_rows_covered_by_new_suppressions(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RINARI_KEYRING", "0")
    authority_ctx = build_app_context(
        home=tmp_path / "authority",
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
    )
    authority = MemoryService(authority_ctx)
    forgotten = authority.remember_user("Prefiero respuestas breves", topic="formato")
    row = authority.get_user(forgotten["id"])
    authority.forget_user(row["id"], expected_revision=row["revision"])
    ledger = authority.export_privacy_ledger()
    digest = authority.privacy_ledger_digest(ledger)
    authority_ctx.close()

    restored_ctx = build_app_context(
        home=tmp_path / "restored",
        clock=FakeClock(start=1_700_000_100.0, step=1.0),
    )
    try:
        restored = MemoryService(restored_ctx)
        stale = restored.remember_user("Prefiero respuestas breves", topic="otro tema")
        assert restored.get_user(stale["id"]) is not None

        receipt = restored.import_privacy_ledger(ledger, digest)
        assert receipt["imported"] is True
        assert restored.get_user(stale["id"]) is None
        assert restored.search_user("respuestas breves") == []
        assert restored.prompt_segment(None) is None
    finally:
        restored_ctx.close()


def test_ledger_import_revokes_source_derived_memory_and_refreshes_projection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RINARI_KEYRING", "0")
    authority_ctx = build_app_context(
        home=tmp_path / "authority-source",
        clock=FakeClock(start=1_700_001_000.0, step=1.0),
    )
    authority = MemoryService(authority_ctx)
    text = "Prefiero respuestas breves"
    _append(authority_ctx, "source-session", "source-message", text)
    captured = authority.capture_owner_message("source-session", "source-message", text)
    assert captured is not None
    authority.forget_user(captured["memory_id"])
    ledger = authority.export_privacy_ledger()
    digest = authority.privacy_ledger_digest(ledger)
    authority_ctx.close()

    restored_ctx = build_app_context(
        home=tmp_path / "restored-source",
        clock=FakeClock(start=1_700_001_100.0, step=1.0),
    )
    try:
        restored = MemoryService(restored_ctx)
        _append(restored_ctx, "source-session", "source-message", text)
        stale = restored.capture_owner_message("source-session", "source-message", text)
        assert stale is not None
        assert restored.get_user(stale["memory_id"]) is not None

        receipt = restored.import_privacy_ledger(ledger, digest)
        assert receipt["ledger_digest"] == digest
        assert restored.get_user(stale["memory_id"]) is None
        assert restored.repo.source_suppressed("source-session", "source-message")
        assert restored.repo.sources_for_session("source-session", live_only=True) == []
    finally:
        restored_ctx.close()


def test_source_only_ledger_purges_paraphrase_and_invalidates_compact_state(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RINARI_KEYRING", "0")
    authority_ctx = build_app_context(
        home=tmp_path / "source-authority",
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
    )
    authority = MemoryService(authority_ctx)
    authority.repo.source_suppression_insert(
        "source-session", "source-message", "2026-01-01T00:00:00Z"
    )
    authority.repo.ledger_advance()
    ledger = authority.export_privacy_ledger()
    assert ledger["memory_suppressions"] == []
    digest = authority.privacy_ledger_digest(ledger)
    authority_ctx.close()

    restored_ctx = build_app_context(
        home=tmp_path / "source-restored",
        clock=FakeClock(start=1_700_000_100.0, step=1.0),
    )
    try:
        memory = MemoryService(restored_ctx)
        now = "2026-01-01T00:00:00Z"
        restored_ctx.db.execute(
            """
            INSERT INTO sessions (
                id, kind, title, created_cwd, current_cwd, provider_id, model_id,
                compact_state_json, created_at, updated_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source-session",
                "CHAT",
                "restored",
                str(tmp_path),
                str(tmp_path),
                "provider-fixture",
                "model-fixture",
                '{"summary":"contains forgotten source"}',
                now,
                now,
                now,
            ),
        )
        _append(
            restored_ctx,
            "source-session",
            "source-message",
            "Prefiero respuestas breves",
        )
        unique = memory.remember_user(
            "Mantén las respuestas concisas",
            topic="estilo derivado",
            source={
                "session_id": "source-session",
                "message_id": "source-message",
                "source_hash": "a" * 64,
                "quote": "",
            },
        )
        shared = memory.remember_user(
            "Usa encabezados descriptivos",
            topic="formato derivado",
            source={
                "session_id": "source-session",
                "message_id": "source-message",
                "source_hash": "a" * 64,
                "quote": "",
            },
        )
        memory.remember_user(
            "Usa encabezados descriptivos",
            topic="formato derivado",
            source={
                "session_id": "independent-session",
                "message_id": "independent-message",
                "source_hash": "b" * 64,
                "quote": "",
            },
        )

        memory.import_privacy_ledger(ledger, digest)

        assert memory.get_user(unique["id"]) is None
        assert memory.get_user(shared["id"]) is not None
        live_sources = memory.repo.sources_for_memory(shared["id"], live_only=True)
        assert [(row["session_id"], row["message_id"]) for row in live_sources] == [
            ("independent-session", "independent-message")
        ]
        assert restored_ctx.session_repo.get("source-session").compact_state is None
    finally:
        restored_ctx.close()


def test_ledger_record_ids_purge_preupdate_and_superseded_backup_rows(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RINARI_KEYRING", "0")
    authority_ctx = build_app_context(
        home=tmp_path / "record-authority",
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
    )
    authority = MemoryService(authority_ctx)
    panel = authority.remember_user("Texto panel v1", topic="panel", provenance="panel")
    panel_row = authority.get_user(panel["id"])
    authority.update_user(
        panel["id"],
        text="Texto panel v2",
        expected_revision=panel_row["revision"],
        owner_consent=True,
    )
    updated = authority.get_user(panel["id"])
    authority.forget_user(panel["id"], expected_revision=updated["revision"])

    lineage_old = authority.remember_user("Versión anterior", topic="lineage")
    lineage_new = authority.remember_user("Versión vigente", topic="lineage")
    current = authority.get_user(lineage_new["id"])
    authority.forget_user(lineage_new["id"], expected_revision=current["revision"])
    ledger = authority.export_privacy_ledger()
    digest = authority.privacy_ledger_digest(ledger)
    suppressed_ids = {row["memory_id"] for row in ledger["record_suppressions"]}
    assert {panel["id"], lineage_old["id"], lineage_new["id"]} <= suppressed_ids
    authority_ctx.close()

    restored_ctx = build_app_context(
        home=tmp_path / "record-restored",
        clock=FakeClock(start=1_700_000_100.0, step=1.0),
    )
    try:
        memory = MemoryService(restored_ctx)
        now = "2026-01-01T00:00:00Z"
        for memory_id, topic, text in (
            (panel["id"], "panel", "Texto panel v1"),
            (lineage_old["id"], "lineage", "Versión anterior"),
        ):
            memory.repo.user_insert(
                {
                    "id": memory_id,
                    "kind": "preference",
                    "topic": topic,
                    "text": text,
                    "provenance": "restored-backup",
                    "confidence": 1.0,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        assert len(memory.list_user()) == 2

        memory.import_privacy_ledger(ledger, digest)
        assert memory.list_user() == []
        assert memory.prompt_segment(None) is None
    finally:
        restored_ctx.close()


def test_live_agent_session_drops_forgotten_source_before_next_model_call(
    app_ctx, tmp_path
) -> None:
    user_home = tmp_path / "user-home"
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
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = services.sessions.start(cwd, forced_chat=True).session
    caller = FakeModel(
        scripted=[
            ModelResponse(content="Lo recordaré", stop_reason=StopReason.END_TURN),
            ModelResponse(content="Continuamos", stop_reason=StopReason.END_TURN),
        ]
    )
    session = build_agent_session(
        services,
        record,
        interactive=False,
        user_home=user_home,
        model_caller=caller,
    )

    first_text = "Prefiero respuestas breves"
    run_turn(session, first_text)
    persisted = services.ctx.message_repo.list(record.id)
    assert all(row.turn_id for row in persisted)
    stored = services.memory.list_user()
    assert len(stored) == 1
    services.memory.forget_user(stored[0]["id"], expected_revision=stored[0]["revision"])

    run_turn(session, "Siguiente pregunta")
    next_contents = [message.content for message in caller.requests[-1].messages if message.content]
    assert first_text not in next_contents
    assert "Lo recordaré" not in next_contents
    assert "Siguiente pregunta" in next_contents


def test_live_agent_session_drops_invalidated_compact_state_before_next_turn(
    app_ctx, tmp_path
) -> None:
    user_home = tmp_path / "compact-user-home"
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
    cwd = tmp_path / "compact-work"
    cwd.mkdir()
    record = services.sessions.start(cwd, forced_chat=True).session
    caller = FakeModel(
        scripted=[
            ModelResponse(content="Lo recordaré", stop_reason=StopReason.END_TURN),
            ModelResponse(content="Continuamos", stop_reason=StopReason.END_TURN),
        ]
    )
    session = build_agent_session(
        services,
        record,
        interactive=False,
        user_home=user_home,
        model_caller=caller,
    )

    forgotten_text = "Prefiero respuestas breves"
    run_turn(session, forgotten_text, turn_id="turn-memory")
    stored = services.memory.list_user()
    assert len(stored) == 1

    latest = services.ctx.session_repo.get(record.id)
    latest.compact_state = CompactState(constraints=(forgotten_text,)).to_dict()
    services.ctx.session_repo.update(latest)
    services.context.restore_compact_state(session.context)
    assert forgotten_text in session.context.compact_state_text

    services.memory.forget_user(stored[0]["id"], expected_revision=stored[0]["revision"])
    assert services.ctx.session_repo.get(record.id).compact_state is None
    assert forgotten_text in session.context.compact_state_text

    run_turn(session, "Siguiente pregunta", turn_id="turn-next")
    next_contents = [message.content for message in caller.requests[-1].messages if message.content]
    assert forgotten_text not in next_contents
    assert session.context.compact_state_text is None
    assert services.ctx.session_repo.get(record.id).compact_state is None
