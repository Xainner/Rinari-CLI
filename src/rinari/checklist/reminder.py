"""A short reminder when the live checklist falls behind the work.

Models often write the list once and never touch it again while they keep
working, so the user watches step 1 «in progress» while files are written.
After a few tool rounds without `checklist.update` on a list that is still
active, the loop adds one harness note with where the list stands. It never
fires on a finished or cleared list, and the count restarts after each
reminder or update, so a long turn gets an occasional nudge, not a stream.
"""

from __future__ import annotations

from rinari.checklist.service import ChecklistService

REMIND_AFTER_ROUNDS = 4


class ChecklistReminder:
    def __init__(
        self,
        service: ChecklistService,
        session_id: str,
        *,
        every: int = REMIND_AFTER_ROUNDS,
    ) -> None:
        self._service = service
        self._session_id = session_id
        self._every = max(1, every)
        self._quiet_rounds = 0

    def after_round(self, tool_names: list[str]) -> str | None:
        """Called after each tool round with the tools it ran; returns a note or None."""
        if "checklist.update" in tool_names:
            self._quiet_rounds = 0
            return None
        try:
            current = self._service.get(self._session_id)
        except Exception:  # a reminder is a courtesy, never a failed turn
            return None
        if current is None or current.state != "active":
            self._quiet_rounds = 0
            return None
        unfinished = [item for item in current.items if item["status"] != "completed"]
        if not unfinished:
            self._quiet_rounds = 0
            return None
        self._quiet_rounds += 1
        if self._quiet_rounds < self._every:
            return None
        self._quiet_rounds = 0
        done = len(current.items) - len(unfinished)
        working = next((item for item in current.items if item["status"] == "in_progress"), None)
        where = f"«{working['content']}» in progress" if working else "no step in progress"
        return (
            f"Checklist check: the user still sees {done}/{len(current.items)} done, "
            f"{where}. If steps finished or started since, send the updated list with "
            "checklist.update now; if not, keep working."
        )
