"""Independent adversarial coverage for the personal-memory increment."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rinari.application.context import build_app_context
from rinari.memory.service import (
    MEM_PROMPT_MAX_CHARS,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryService,
)
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError
from rinari.tools.native.memory import memory_remember, memory_update
from tests.unit.test_tool_runtime import _ctx


def test_revision_cas_survives_identical_timestamps(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("RINARI_KEYRING", "0")
    ctx = build_app_context(home=home, clock=FakeClock(step=0.0))
    try:
        memory = MemoryService(ctx)
        created = memory.remember_user("Primera", topic="estilo")
        original = memory.get_user(created["id"])

        current = memory.update_user(
            created["id"], text="Segunda", expected_revision=original["revision"]
        )
        assert current["updated_at"] == original["updated_at"]
        assert current["revision"] == original["revision"] + 1

        with pytest.raises(MemoryConflictError):
            memory.update_user(
                created["id"], text="Tercera", expected_revision=original["revision"]
            )
        with pytest.raises(MemoryConflictError):
            memory.forget_user(created["id"], expected_revision=original["revision"])
        assert memory.get_user(created["id"])["text"] == "Segunda"
    finally:
        ctx.close()


def test_suppression_is_durable_and_topic_change_is_not_a_bypass(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("RINARI_KEYRING", "0")
    ctx = build_app_context(home=home, clock=FakeClock(step=0.0))
    memory = MemoryService(ctx)
    created = memory.remember_user("Respuesta breve", topic="Estilo")
    duplicate = memory.remember_user("Respuesta breve", topic="Longitud")
    row = memory.get_user(created["id"])
    assert memory.forget_user(created["id"], expected_revision=row["revision"])
    assert memory.get_user(duplicate["id"]) is None
    assert memory.search_user("Respuesta breve") == []
    assert "Respuesta breve" not in (memory.prompt_segment(None) or "")
    suppression = dict(ctx.db.query_one("SELECT * FROM memory_suppressions"))
    assert set(suppression) == {"topic_hash", "text_hash", "created_at"}
    assert "Estilo" not in suppression.values()
    assert "Respuesta breve" not in suppression.values()
    ctx.close()

    reopened = build_app_context(home=home, clock=FakeClock(step=0.0))
    try:
        memory = MemoryService(reopened)
        with pytest.raises(InvalidUsageError) as rejected:
            memory.remember_user("  respuesta   BREVE ", topic=" estilo ")
        message = str(rejected.value).lower()
        assert "new wording" not in message
        assert "restore" not in message

        # Relabelling the same forgotten content must not bypass suppression.
        with pytest.raises(InvalidUsageError):
            memory.remember_user("Respuesta breve", topic="longitud")
    finally:
        reopened.close()


def test_existing_tools_cannot_recreate_or_update_into_suppressed_pair(
    app_ctx, tmp_path
) -> None:
    memory = MemoryService(app_ctx)
    forgotten = memory.remember_user("Usa español", topic="idioma")
    forgotten_row = memory.get_user(forgotten["id"])
    memory.forget_user(forgotten["id"], expected_revision=forgotten_row["revision"])
    live = memory.remember_user("Responde breve", topic="estilo")

    root = tmp_path / "project"
    root.mkdir()
    tool_ctx = replace(_ctx(tmp_path, root), memory=memory)
    recreate = memory_remember(
        {"scope": "user", "topic": "idioma", "text": "Usa español"}, tool_ctx
    )
    assert not recreate.ok
    update = memory_update(
        {
            "scope": "user",
            "id": live["id"],
            "topic": "idioma",
            "text": "Usa español",
        },
        tool_ctx,
    )
    assert not update.ok
    assert memory.get_user(live["id"])["text"] == "Responde breve"


@pytest.mark.parametrize(
    "arguments",
    [
        {"scope": "user", "topic": "t" * 129, "text": "válido"},
        {
            "scope": "user",
            "topic": "válido",
            "text": "válido",
            "provenance": "p" * 257,
        },
        {"scope": "user", "topic": "válido", "text": "válido", "confidence": True},
        {
            "scope": "user",
            "topic": "token=abcdefghijklmnop",
            "text": "válido",
        },
    ],
)
def test_tool_rejects_oversized_invalid_or_secret_metadata(app_ctx, tmp_path, arguments) -> None:
    root = tmp_path / "project"
    root.mkdir()
    tool_ctx = replace(_ctx(tmp_path, root), memory=MemoryService(app_ctx))
    result = memory_remember(arguments, tool_ctx)
    assert not result.ok
    assert MemoryService(app_ctx).list_user() == []


def test_superseded_record_is_not_addressable_or_mutable(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    first = memory.remember_user("pytest", topic="tests", kind="fact")
    memory.remember_user("uv run pytest", topic="tests", kind="fact")

    assert memory.get_user(first["id"]) is None
    with pytest.raises(MemoryNotFoundError):
        memory.update_user(first["id"], text="hidden", expected_revision=1)
    assert memory.forget_user(first["id"], expected_revision=1) is False


def test_reopened_context_keeps_user_memory_and_separates_projects(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("RINARI_KEYRING", "0")
    first = build_app_context(home=home, clock=FakeClock(step=0.0))
    memory = MemoryService(first)
    memory.remember_user("Prefiere español", topic="idioma")
    memory.remember_project("/project-a", "Solo A", topic="scope")
    memory.remember_project("/project-b", "Solo B", topic="scope")
    first.close()

    second = build_app_context(home=home, clock=FakeClock(step=0.0))
    try:
        memory = MemoryService(second)
        chat = memory.prompt_segment(None) or ""
        project_a = memory.prompt_segment("/project-a") or ""
        assert "Prefiere español" in chat
        assert "Solo A" not in chat and "Solo B" not in chat
        assert "Prefiere español" in project_a and "Solo A" in project_a
        assert "Solo B" not in project_a
    finally:
        second.close()


def test_prompt_retrieval_prefers_old_relevant_memory_within_budget(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    relevant = memory.remember_user("Saturno usa una GPU NVIDIA", topic="hardware saturno")
    for index in range(20):
        memory.remember_user(f"nota reciente {index}", topic=f"nota-{index}")

    rendered = memory.prompt_segment(None, query="revisa el hardware de saturno") or ""
    assert memory.get_user(relevant["id"])["text"] in rendered
    assert len(rendered) <= MEM_PROMPT_MAX_CHARS


def test_relevant_project_memory_is_not_starved_by_unrelated_user_rows(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    project_root = "C:/project-a"
    memory.remember_project(
        project_root,
        "Saturno usa una GPU NVIDIA para inferencia",
        topic="hardware saturno",
    )
    for index in range(15):
        memory.remember_user(
            f"nota sin relación {index} " + ("x" * 760), topic=f"nota-{index}"
        )

    rendered = memory.prompt_segment(project_root, query="hardware de saturno") or ""
    assert "Saturno usa una GPU NVIDIA" in rendered
    assert len(rendered) <= MEM_PROMPT_MAX_CHARS
