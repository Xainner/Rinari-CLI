"""«In this chat» / «in this session» grants, kept with the conversation (plan 0.4.0 #56).

They lived in memory and were lost when the app or the Engine restarted. Now
each one is a row of `session_approval_grants`, read back when the
conversation runs again and deleted with it (foreign key cascade). The list
is the one the turn's ApprovalEngine appends to, so nothing else changes.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from rinari.policy.approvals import ApprovalGrant, GrantScope


class SessionGrants(list):
    def __init__(self, db: Any, session_id: str) -> None:
        super().__init__()
        self._db = db
        self._session_id = session_id
        rows: list = []
        with contextlib.suppress(sqlite3.Error):
            rows = db.query(
                "SELECT * FROM session_approval_grants WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        for row in rows:
            super().append(
                ApprovalGrant(
                    capability=row["capability"],
                    scope=GrantScope.SESSION,
                    target=row["target"],
                    session_id=session_id,
                    project_id=row["project_id"],
                    granted_at=row["granted_at"],
                    rule_id=row["rule_id"],
                )
            )

    def append(self, grant: ApprovalGrant) -> None:  # type: ignore[override]
        super().append(grant)
        if grant.scope is not GrantScope.SESSION or grant.session_id != self._session_id:
            return
        # A conversation that is not stored (a remote peer) keeps it in memory.
        with contextlib.suppress(sqlite3.Error):
            self._db.execute(
                "INSERT INTO session_approval_grants "
                "(session_id, capability, rule_id, target, project_id, granted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._session_id,
                    grant.capability,
                    grant.rule_id,
                    grant.target,
                    grant.project_id,
                    grant.granted_at or datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )


class SessionGrantRegistry:
    """One grant list per conversation, shared by every turn that runs in it.

    The turn's ApprovalEngine appends to the list it is given, so a session
    keeps the same object for as long as the Engine runs; `forget` drops the
    cached copy when the rows changed underneath (a child inherits its
    mother's grants by inserting rows).
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._lists: dict[str, SessionGrants] = {}

    def for_session(self, session_id: str) -> SessionGrants:
        with self._lock:
            grants = self._lists.get(session_id)
            if grants is None:
                grants = self._lists[session_id] = SessionGrants(self._db, session_id)
            return grants

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._lists.pop(session_id, None)
