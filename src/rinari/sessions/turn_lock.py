"""Cross-process exclusive lease for one active turn per session."""

from __future__ import annotations

import os
from collections.abc import Callable
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


LOCK_SUFFIX = ".turn.lock"


def lock_path(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / f"{session_id}{LOCK_SUFFIX}"


def discard_lock(path: Path, session_id: str) -> bool:
    """Remove a lock file nobody holds; True when it is gone.

    Files were created per session and never removed, so they piled up for
    sessions long deleted. Removal is only safe for a lock that is free, and
    only meant for sessions that no longer exist (no new turn can start
    there): POSIX unlinks while holding the lock, so a racing process either
    sees it held or opens a file nobody will ever use again; Windows cannot
    delete a file another process has open, so a held lock simply stays.
    """
    if not path.exists():
        return True
    try:
        with SessionTurnLock(path, session_id):
            if os.name != "nt":
                path.unlink(missing_ok=True)
    except (ConflictError, OSError):
        return False
    if os.name == "nt":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False  # Opened again in between: it is in use, keep it.
    return True


def sweep_orphan_locks(sessions_dir: Path, live_session_ids: Callable[[], set[str]]) -> int:
    """Discard the free lock files of sessions that no longer exist.

    The files are listed before the live sessions are read: a session row
    always exists before its first lock file, so a session created during the
    sweep is never mistaken for a deleted one.
    """
    if not sessions_dir.is_dir():
        return 0
    candidates = list(sessions_dir.glob(f"*{LOCK_SUFFIX}"))
    if not candidates:
        return 0
    live = live_session_ids()
    removed = 0
    for path in candidates:
        session_id = path.name[: -len(LOCK_SUFFIX)]
        if session_id not in live and discard_lock(path, session_id):
            removed += 1
    return removed


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


__all__ = ["SessionTurnLock", "discard_lock", "lock_path", "sweep_orphan_locks"]
