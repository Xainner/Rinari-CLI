"""A short reminder when the live checklist falls behind the work.

Models often write the list once and never touch it again while they keep
working, so the user watches step 1 «in progress» while files are written.
After a few tool rounds without `checklist.update` on a list that is still
active, the loop adds one harness note with where the list stands. It never
fires on a finished or cleared list, and the count restarts after each
reminder or update, so a long turn gets an occasional nudge, not a stream.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from rinari.checklist.service import ChecklistService

REMIND_AFTER_ROUNDS = 4
#: Rounds without any list before suggesting one (plan 0.4.0 #48).
START_AFTER_ROUNDS = 6
WRITE_TOOLS = frozenset({"fs.write", "fs.patch", "fs.delete", "fs.move", "fs.mkdir"})
JOB_TOOLS = frozenset({"process.start", "agent.spawn", "documents.render", "web.download"})
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+\S", re.MULTILINE)
_SEQUENCE = re.compile(
    r"\b(?:y luego|y después|después de eso|por último|primero\b.*\b(?:luego|después)"
    r"|and then|afterwards|finally|first\b.*\bthen)\b",
    re.IGNORECASE | re.DOTALL,
)
_QUANTITY = re.compile(r"\b\d+\s+[^\W\d_]{3,}", re.UNICODE)
_FILE = re.compile(r"\b[\w-]+\.[A-Za-z0-9]{2,5}\b")


def multi_step_request(text: str) -> bool:
    """The request names several things to do: a list, «y luego», several
    quantities («5 imágenes, 5 videos») or several files."""
    if not text:
        return False
    if len(_LIST_ITEM.findall(text)) >= 2 or _SEQUENCE.search(text):
        return True
    if len(_QUANTITY.findall(text)) >= 2:
        return True
    return len(set(_FILE.findall(text))) >= 2


class ChecklistReminder:
    def __init__(
        self,
        service: ChecklistService,
        session_id: str,
        *,
        every: int = REMIND_AFTER_ROUNDS,
        mode: Callable[[], str] | None = None,
        request: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._session_id = session_id
        self._every = max(1, every)
        self._quiet_rounds = 0
        self._mode = mode
        self._request = request
        # The suggestion to start a list, once per turn (#48).
        self._turn: str | None = None
        self._bare_rounds = 0
        self._writes = 0
        self._jobs = 0
        self._suggested = False

    def before_final(self) -> str | None:
        """When the model is about to answer with steps still open: one note.

        The model often does the last steps and answers without touching the
        list again, so the owner sees «Quedan 3 pasos» on finished work (plan
        0.4.0 #59). The loop asks this once per turn; with the list up to date
        it returns None and nothing happens.
        """
        try:
            current = self._service.get(self._session_id)
        except Exception:  # a check is a courtesy, never a failed turn
            return None
        if current is None or current.state != "active":
            return None
        unfinished = [item for item in current.items if item["status"] != "completed"]
        if not unfinished:
            return None
        states = {"in_progress": "in progress", "pending": "pending", "blocked": "blocked"}
        listed = "; ".join(
            f"«{item['content']}» {states.get(item['status'], item['status'])}"
            for item in unfinished[:6]
        )
        return (
            f"Checklist check before you finish: the user's list still says {listed}. "
            "If those steps are done, mark them with checklist.update and add nothing else: "
            "your answer above stands. If something really is left, update the list and say "
            "what is left in one line."
        )

    def _start_note(self, tool_names: list[str]) -> str | None:
        """No list yet: suggest one, once per turn, only when the work is
        several steps (plan 0.4.0 #48). A question or a chat never gets it."""
        if self._suggested or self._mode is None:
            return None
        self._bare_rounds += 1
        self._writes += sum(1 for name in tool_names if name in WRITE_TOOLS)
        self._jobs += sum(1 for name in tool_names if name in JOB_TOOLS)
        if self._bare_rounds < START_AFTER_ROUNDS:
            return None
        try:
            if self._mode() not in ("build", "plan"):
                return None
            request = self._request() if self._request is not None else ""
        except Exception:
            return None
        if not (multi_step_request(request) or self._writes >= 2 or self._jobs >= 2):
            return None
        self._suggested = True
        return (
            "This is several steps of work and the user has no checklist to follow it. "
            "Write one with checklist.update now (one item in_progress), then keep working "
            "and keep it up to date."
        )

    def after_round(self, tool_names: list[str]) -> str | None:
        """Called after each tool round with the tools it ran; returns a note or None."""
        turn = getattr(self._service, "_current_turn", {}).get(self._session_id)
        if turn != self._turn:
            # A new turn: the suggestion to start a list may come once more (#48).
            self._turn = turn
            self._bare_rounds = self._writes = self._jobs = 0
            self._suggested = False
        if "checklist.update" in tool_names:
            self._quiet_rounds = 0
            self._suggested = True
            return None
        try:
            current = self._service.get(self._session_id)
        except Exception:  # a reminder is a courtesy, never a failed turn
            return None
        if current is None or current.state in ("cleared", "completed"):
            self._quiet_rounds = 0
            return self._start_note(tool_names)
        if current.state != "active":
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
