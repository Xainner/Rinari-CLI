from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.policy.engine import PermissionProfile
from rinari.storage.records import SessionRecord


@pytest.fixture
def changes_runtime(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user_home.mkdir()
    workspace.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    provider = services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="not-a-real-secret",
        )
    )
    model = services.models.add("fake", "fake-model", "fake-model")
    now = "2026-01-01T00:00:00Z"
    record = SessionRecord(
        id="ses_changes",
        kind="CHAT",
        title="changes",
        project_id=None,
        project_root_snapshot=None,
        created_cwd=str(workspace),
        current_cwd=str(workspace),
        provider_id=provider.id,
        model_id=model.id,
        profile_id="default",
        mode="build",
        state="active",
        compact_state=None,
        created_at=now,
        updated_at=now,
        last_active_at=now,
        permission_profile="full-access",
    )
    app_ctx.session_repo.insert(record)
    context = SimpleNamespace(
        cwd=workspace,
        kind="CHAT",
        project_root=None,
        profile=PermissionProfile.FULL_ACCESS,
        user_home=user_home,
        worktree=None,
        private_roots=(),
    )
    return services, record, context, workspace


def test_created_file_is_reviewable_and_conflict_safe_to_undo(changes_runtime) -> None:
    services, record, context, workspace = changes_runtime
    target = workspace / "created.txt"
    tracker = services.changes.begin(services, record, "turn_created")

    observation = tracker.before_tool("fs.write", {"path": str(target)}, context)
    target.write_text("hello\n", encoding="utf-8")
    tracker.after_tool(observation)
    changeset = tracker.finalize()

    assert changeset is not None
    assert changeset["files"][0]["kind"] == "created"
    assert "before_blob_ref" not in changeset["files"][0]
    review = services.changes.review("turn_created")
    assert "+hello" in review["files"][0]["diff"]
    result = services.changes.undo("turn_created")
    assert result["status"] == "undone"
    assert not target.exists()


def test_divergent_file_blocks_total_undo_and_is_audited(changes_runtime) -> None:
    services, record, context, workspace = changes_runtime
    target = workspace / "existing.txt"
    target.write_text("before\n", encoding="utf-8")
    tracker = services.changes.begin(services, record, "turn_conflict")

    observation = tracker.before_tool("fs.patch", {"path": str(target)}, context)
    target.write_text("agent\n", encoding="utf-8")
    tracker.after_tool(observation)
    tracker.finalize()
    target.write_text("user-after\n", encoding="utf-8")

    result = services.changes.undo("turn_conflict")
    assert result["status"] == "conflicted"
    assert result["applied"] == []
    assert result["conflicts"][0]["reason"] == "changed_after_turn"
    assert target.read_text(encoding="utf-8") == "user-after\n"
    row = services.ctx.db.query_one(
        "SELECT status FROM turn_change_undo_operations WHERE id = ?",
        (result["undo_operation_id"],),
    )
    assert row is not None and row["status"] == "conflicted"


def test_sensitive_file_never_exposes_content_or_supports_undo(changes_runtime) -> None:
    services, record, context, workspace = changes_runtime
    target = workspace / ".env"
    target.write_text("SECRET=before\n", encoding="utf-8")
    tracker = services.changes.begin(services, record, "turn_secret")

    observation = tracker.before_tool("fs.write", {"path": str(target)}, context)
    target.write_text("SECRET=after\n", encoding="utf-8")
    tracker.after_tool(observation)
    changeset = tracker.finalize()

    assert changeset is not None
    changed = services.changes.review("turn_secret")["files"][0]
    assert changed["sensitive"] is True
    assert changed["diff"] is None
    assert changed["undoable"] is False
    assert changed["conflict_reason"] == "sensitive_file"


def test_read_only_shell_does_not_create_changeset(changes_runtime) -> None:
    services, record, context, _workspace = changes_runtime
    tracker = services.changes.begin(services, record, "turn_read")
    observation = tracker.before_tool("shell.exec", {"command": "git status"}, context)
    assert observation is None
    assert tracker.finalize() is None


def test_shell_rename_is_one_change_and_can_be_reversed(changes_runtime) -> None:
    services, record, context, workspace = changes_runtime
    source = workspace / "before.txt"
    target = workspace / "after.txt"
    source.write_text("same content\n", encoding="utf-8")
    tracker = services.changes.begin(services, record, "turn_rename")

    observation = tracker.before_tool(
        "shell.exec", {"command": "Move-Item before.txt after.txt"}, context
    )
    source.rename(target)
    tracker.after_tool(observation)
    changeset = tracker.finalize()

    assert changeset is not None
    assert [(row["kind"], row["path"]) for row in changeset["files"]] == [("renamed", str(target))]
    result = services.changes.undo("turn_rename")
    assert result["status"] == "undone"
    assert source.read_text(encoding="utf-8") == "same content\n"
    assert not target.exists()


def test_protocol_reads_changes_and_timeline_keeps_it_nonterminal(changes_runtime) -> None:
    services, record, context, workspace = changes_runtime
    target = workspace / "protocol.txt"
    tracker = services.changes.begin(services, record, "turn_protocol")
    observation = tracker.before_tool("fs.write", {"path": str(target)}, context)
    target.write_text("protocol\n", encoding="utf-8")
    tracker.after_tool(observation)
    changeset = tracker.finalize()
    assert changeset is not None
    server = EngineServer(services)
    server.turns.emit_persisted_activity(
        "turn.changes.completed",
        session_id=record.id,
        turn_id="turn_protocol",
        payload=changeset,
    )

    response = server.handle_line(
        json.dumps(
            {
                "id": "req_changes",
                "method": "turn.changes.get",
                "params": {"turn_id": "turn_protocol"},
            }
        )
    )
    assert response is not None and response["ok"] is True
    assert response["result"]["files"][0]["path"] == str(target)
    timeline = server.handle_line(
        json.dumps(
            {
                "id": "req_timeline",
                "method": "session.timeline",
                "params": {"ref": record.id},
            }
        )
    )
    assert timeline is not None and timeline["ok"] is True
    turn = timeline["result"]["turns"][0]
    assert turn["status"] == "running"
    assert turn["items"][0]["event"] == "turn.changes.completed"


def test_private_restore_blob_is_content_address_verified(changes_runtime) -> None:
    services, _record, _context, _workspace = changes_runtime
    reference = services.changes.blobs.put(b"trusted")
    target = services.changes.blobs.root / reference[:2] / reference
    target.write_bytes(b"tampered")
    with pytest.raises(OSError, match="integrity"):
        services.changes.blobs.read(reference)
