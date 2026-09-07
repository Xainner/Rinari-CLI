"""Cross-process exclusive lease for one active turn per session."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from rinari.shared.errors import ConflictError


class SessionTurnLock:
    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self._handle: BinaryIO | None = None

    def __enter__(self) -> SessionTurnLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            _lock(handle)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise ConflictError(
                f"Session {self.session_id} already has an active turn.",
                hint="Wait for it to finish or use `rinari stop` before starting another turn.",
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None


if os.name == "nt":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["SessionTurnLock"]
