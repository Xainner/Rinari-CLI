"""«In this chat» / «in this session» grants, kept with the conversation (plan 0.4.0 #56).

They lived in memory and were lost when the app or the Engine restarted. Now
each one is a row of `session_approval_grants`, read back when the
conversation runs again and deleted with it (foreign key cascade). The list
is the one the turn's ApprovalEngine appends to, so nothing else changes.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from typing import Any

from rinari.policy.approvals import ApprovalGrant, GrantScope

# Grants a board's peer group gives; revoking the group takes them back.
PEER_CAPABILITY = "session.message"


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


def drop_peer_grants(db: Any, session_id: str) -> None:
    """A revoked peer group takes back what it allowed, and nothing else."""
    with contextlib.suppress(sqlite3.Error):
        db.execute(
            "DELETE FROM session_approval_grants WHERE session_id = ? AND capability = ?",
            (session_id, PEER_CAPABILITY),
        )
