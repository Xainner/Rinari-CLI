"""SQLite connection layer: WAL mode, FK enforcement, explicit transactions."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Database:
    """Thin wrapper over sqlite3 with autocommit off for explicit TX control.

    Statements are serialized with a reentrant lock: subagent worker threads
    persist lifecycle events on the same connection as the main thread.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Per-thread so a worker thread can't see the main thread's depth and
        # skip BEGIN IMMEDIATE (which would overlap two transactions).
        self._tx_depth_local = threading.local()
        self._set_pragmas()

    def _set_pragmas(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, list(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return list(cursor.fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return cursor.fetchone()

    def _depth(self) -> int:
        return getattr(self._tx_depth_local, "depth", 0)

    def in_transaction(self) -> bool:
        return self._depth() > 0

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        # Hold the lock across the whole transaction so other threads cannot
        # interleave with (or BEGIN inside) this one. Same-thread nesting is
        # allowed (RLock); the outermost level owns BEGIN/COMMIT.
        with self._lock:
            depth = self._depth() + 1
            self._tx_depth_local.depth = depth
            if depth == 1:
                self.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                if self._depth() == 1:
                    self.execute("ROLLBACK")
                raise
            else:
                if self._depth() == 1:
                    self.execute("COMMIT")
            finally:
                self._tx_depth_local.depth = depth - 1

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
