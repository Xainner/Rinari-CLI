"""Personal memory written by the model during the owner's turn."""

from __future__ import annotations

from dataclasses import replace

from rinari.memory.service import MemoryService
from rinari.storage.records import SessionMessageRecord
from rinari.tools.definition import ToolErrorCode
from rinari.tools.native.memory import memory_remember
from tests.unit.test_tool_runtime import _ctx


def _owner_turn(app_ctx, tmp_path, text: str):
    app_ctx.message_repo.append_many(
        "owner",
        [SessionMessageRecord(id="m1", session_id="owner", seq=0, role="user", content=text)],
    )
    memory = MemoryService(app_ctx)
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    ctx = replace(
        _ctx(tmp_path, root),
        session_id="owner",
        memory=memory,
        memory_source={"session_id": "owner", "message_id": "m1", "text": text},
    )
    return memory, ctx


def test_the_model_words_the_memory_and_it_points_at_the_owner_message(app_ctx, tmp_path) -> None:
    memory, ctx = _owner_turn(
        app_ctx, tmp_path, "Guarda en memoria: casa3090 corre el bot Together"
    )
    result = memory_remember(
        {
            "scope": "user",
            "topic": "casa3090",
            "text": "casa3090 aloja el bot de Discord Together.",
        },
        ctx,
    )
    assert result.ok, result.error
    [row] = memory.list_user()
    assert row["text"] == "casa3090 aloja el bot de Discord Together."
    assert row["provenance"] == "session:owner/message:m1"
    # The end-of-turn extractor does not store the literal sentence again.
    assert (
        memory.capture_owner_message(
            "owner", "m1", "Guarda en memoria: casa3090 corre el bot Together"
        )
        is None
    )
    assert len(memory.list_user()) == 1


def test_without_an_owner_turn_the_refusal_says_not_to_retry(app_ctx, tmp_path) -> None:
    memory, ctx = _owner_turn(app_ctx, tmp_path, "hola")
    result = memory_remember(
        {"scope": "user", "topic": "x", "text": "algo"}, replace(ctx, memory_source=None)
    )
    assert not result.ok
    # Not APPROVAL_REQUIRED: there is nothing pending the owner could accept.
    assert result.error.code is ToolErrorCode.PERMISSION_DENIED
    assert "Do not retry" in result.error.message
    assert memory.list_user() == []


def test_sensitive_data_becomes_a_proposal_the_owner_approves(app_ctx, tmp_path) -> None:
    memory, ctx = _owner_turn(app_ctx, tmp_path, "Recuerda que tomo insulina en la mañana")
    result = memory_remember(
        {"scope": "user", "topic": "salud", "text": "Toma insulina por la mañana."}, ctx
    )
    assert result.ok and result.data["pending"] is True
    assert "do not retry" in result.data["message"]
    assert memory.list_user() == []
    candidate = result.data["candidate"]
    assert candidate["text"] == "Toma insulina por la mañana."
    resolved = memory.resolve_candidate(candidate["id"], "allow_once")
    assert resolved["status"] == "accepted"
    assert [row["text"] for row in memory.list_user()] == ["Toma insulina por la mañana."]


def test_a_secret_is_refused_and_not_retried(app_ctx, tmp_path) -> None:
    memory, ctx = _owner_turn(app_ctx, tmp_path, "Guarda mi token")
    result = memory_remember(
        {"scope": "user", "topic": "api", "text": "api_key = sk-live-1234567890abcdef1234"}, ctx
    )
    assert not result.ok
    assert result.error.code is ToolErrorCode.VALIDATION_FAILED
    assert "Do not retry" in result.error.message
    assert memory.list_user() == []
