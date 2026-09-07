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
