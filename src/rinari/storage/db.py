"""SQLite connection layer: WAL mode, FK enforcement, explicit transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Database:
    """Thin wrapper over sqlite3 with autocommit off for explicit TX control."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self._conn.executemany(sql, list(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cursor = self._conn.execute(sql, params)
        return list(cursor.fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cursor = self._conn.execute(sql, params)
        return cursor.fetchone()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        self.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.execute("ROLLBACK")
            raise
        else:
            self.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
