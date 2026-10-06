"""The branch a git worktree was on the last time Rinari worked in it.

The resume warning compares the checkout with this observation, not with the
branch a conversation started on: work that began on A, moved to B and ended
on B has nothing to report when it is reopened on B. A change made outside
that work (B -> C) is reported once, with the reference it is measured from.

Observations are written at the start and end of every turn in a PROJECT
session, from any conversation in that worktree. Opening a conversation to
read it is not work and does not move the reference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.projects._git_process import capture_git
from rinari.shared.clock import Clock, now_iso
from rinari.storage.db import Database

SOURCE_INIT = "init"
SOURCE_TURN_START = "turn.start"
SOURCE_TURN_END = "turn.end"


@dataclass(frozen=True, slots=True)
class Checkout:
    worktree: str
    branch: str | None
    head: str

    @property
    def label(self) -> str:
        return self.branch if self.branch else f"detached HEAD at {self.head[:7]}"


def observe_checkout(root: Path) -> Checkout | None:
    """Worktree, branch and HEAD in one git call; None when git cannot tell."""
    output = capture_git(
        root, ["rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD"], timeout_s=10.0
    )
    if output is None:
        return None
    lines = [line.strip() for line in output.strip().splitlines()]
    if len(lines) != 3 or not all(lines):
        return None
    toplevel, head, branch = lines
    worktree = os.path.normcase(str(Path(toplevel).resolve()))
    return Checkout(worktree=worktree, branch=None if branch == "HEAD" else branch, head=head)


class BranchTracker:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def last(self, worktree: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM worktree_branches WHERE worktree = ?", (worktree,))
        return dict(row) if row is not None else None

    def record_work(
        self, root: Path, *, session_id: str | None, turn_id: str | None, source: str
    ) -> Checkout | None:
        """Store the checkout as the reference for the next comparison.

        Git unavailable keeps the previous observation: it is never replaced
        by "unknown". A later observation never loses to an earlier one.
        """
        checkout = observe_checkout(root)
        if checkout is None:
            return None
        observed_at = now_iso(self._clock)
        self._db.execute(
            "INSERT INTO worktree_branches "
            "(worktree, branch, head, observed_at, session_id, turn_id, source, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(worktree) DO UPDATE SET branch = excluded.branch, "
            "head = excluded.head, observed_at = excluded.observed_at, "
            "session_id = excluded.session_id, turn_id = excluded.turn_id, "
            "source = excluded.source, notified = NULL "
            "WHERE excluded.observed_at >= worktree_branches.observed_at",
            (
                checkout.worktree,
                checkout.branch,
                checkout.head,
                observed_at,
                session_id,
                turn_id,
                source,
            ),
        )
        return checkout

    def transition(self, root: Path) -> dict[str, Any] | None:
        """The change since the last observation, reported once per target.

        The first time a worktree is seen its checkout becomes the reference
        silently: older conversations only know the branch they started on,
        which is not evidence of where the work last was.
        """
        checkout = observe_checkout(root)
        if checkout is None:
            return None
        last = self.last(checkout.worktree)
        if last is None:
            self._db.execute(
                "INSERT OR IGNORE INTO worktree_branches "
                "(worktree, branch, head, observed_at, source) VALUES (?, ?, ?, ?, ?)",
                (
                    checkout.worktree,
                    checkout.branch,
                    checkout.head,
                    now_iso(self._clock),
                    SOURCE_INIT,
                ),
            )
            return None
        previous = Checkout(last["worktree"], last["branch"], last["head"] or "")
        same = (
            previous.branch == checkout.branch
            if previous.branch or checkout.branch
            else previous.head == checkout.head
        )
        if same or last["notified"] == checkout.label:
            return None
        self._db.execute(
            "UPDATE worktree_branches SET notified = ? WHERE worktree = ?",
            (checkout.label, checkout.worktree),
        )
        return {
            "from": previous.label,
            "to": checkout.label,
            "since": last["observed_at"],
            "reference": "first_seen" if last["source"] == SOURCE_INIT else "last_work",
            "session_id": last["session_id"],
        }


__all__ = ["BranchTracker", "Checkout", "observe_checkout"]
