"""What a conversation keeps between turns so its requests start the same.

The desktop builds a runtime per turn. The tool list (F42) and where the
settled part of the history ends (F45) have to survive that, or every turn
would decide them again and the provider's prompt cache would start over.
Kept in one small file per session, next to its turn lock; losing it costs one
cache miss, never the conversation.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def state_path(layout, session_id: str) -> Path:
    return layout.dir("sessions") / f"{session_id}.state.json"


class SessionState:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    def _load(self) -> dict[str, Any]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, section: str) -> Any:
        with _LOCK:
            return self._load().get(section)

    def put(self, section: str, value: Any) -> None:
        if self._path is None:
            return
        from rinari.tools.atomic import replace_text

        with _LOCK:
            data = self._load()
            if data.get(section) == value:
                return
            data[section] = value
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                replace_text(self._path, json.dumps(data, ensure_ascii=False))
            except OSError:
                logging.getLogger(__name__).warning("Session state not saved", exc_info=True)


def discard(layout, session_id: str) -> None:
    with _LOCK:
        state_path(layout, session_id).unlink(missing_ok=True)


__all__ = ["SessionState", "discard", "state_path"]
