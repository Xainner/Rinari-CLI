from __future__ import annotations

import pytest

from rinari.sessions.turn_lock import SessionTurnLock
from rinari.shared.errors import ConflictError


def test_session_turn_lock_rejects_concurrent_holder(tmp_path) -> None:
    path = tmp_path / "session.turn.lock"
    with (
        SessionTurnLock(path, "session-1"),
        pytest.raises(ConflictError, match="active turn"),
        SessionTurnLock(path, "session-1"),
    ):
        pass

    with SessionTurnLock(path, "session-1"):
        pass


def test_a_free_lock_is_discarded_and_a_held_one_is_kept(tmp_path) -> None:
    from rinari.sessions.turn_lock import discard_lock, lock_path

    free = lock_path(tmp_path, "ses_free")
    with SessionTurnLock(free, "ses_free"):
        pass
    assert discard_lock(free, "ses_free") and not free.exists()
    held = lock_path(tmp_path, "ses_held")
    with SessionTurnLock(held, "ses_held"):
        assert not discard_lock(held, "ses_held")
        assert held.exists()
    # Still a working lock afterwards.
    with SessionTurnLock(held, "ses_held"):
        pass


def test_the_sweep_keeps_live_sessions_and_reads_them_after_listing(tmp_path) -> None:
    from rinari.sessions.turn_lock import lock_path, sweep_orphan_locks

    for session_id in ("ses_live", "ses_gone", "ses_busy"):
        with SessionTurnLock(lock_path(tmp_path, session_id), session_id):
            pass
    seen: list[bool] = []

    def live() -> set[str]:
        # The files were already listed when the sessions are read.
        seen.append(lock_path(tmp_path, "ses_gone").exists())
        return {"ses_live"}

    with SessionTurnLock(lock_path(tmp_path, "ses_busy"), "ses_busy"):
        assert sweep_orphan_locks(tmp_path, live) == 1
    assert seen == [True]
    assert lock_path(tmp_path, "ses_live").exists()
    assert not lock_path(tmp_path, "ses_gone").exists()
    assert lock_path(tmp_path, "ses_busy").exists()
    assert sweep_orphan_locks(tmp_path / "missing", live) == 0


def test_deleting_a_session_removes_its_lock_and_engine_start_sweeps_the_rest(
    app_ctx, tmp_path
) -> None:
    from rinari.application.services import build_services
    from rinari.engine_protocol.server import EngineServer
    from rinari.sessions.turn_lock import lock_path
    from rinari.storage.records import SessionRecord

    services = build_services(app_ctx, user_home=tmp_path / "home")
    sessions = app_ctx.layout.dir("sessions")
    now = "2026-10-08T00:00:00Z"
    record = SessionRecord(
        id="ses_to_delete",
        kind="CHAT",
        title="t",
        project_id=None,
        project_root_snapshot=None,
        created_cwd=str(tmp_path),
        current_cwd=str(tmp_path),
        provider_id="",
        model_id="",
        profile_id="default",
        mode="build",
        state="active",
        compact_state=None,
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )
    app_ctx.session_repo.insert(record)
    path = lock_path(sessions, record.id)
    with SessionTurnLock(path, record.id):
        pass
    services.sessions.delete(record.id)
    assert not path.exists()
    stale = lock_path(sessions, "ses_deleted_long_ago")
    with SessionTurnLock(stale, "ses_deleted_long_ago"):
        pass
    EngineServer(services, user_home=tmp_path / "home").close()
    assert not stale.exists()
