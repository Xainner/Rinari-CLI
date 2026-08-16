"""Versioned, transactional SQL migrations.

Migrations are numbered .sql files (NNNN_name.sql) applied in order.
Each migration runs in a single transaction; a failure rolls back all
of its statements and leaves the schema_migrations record untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rinari.shared.clock import Clock, iso_utc
from rinari.shared.errors import ConfigurationError
from rinari.storage.db import Database

_MIGRATION_FILE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")
_DEFAULT_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    base = directory or _DEFAULT_DIR
    found: list[Migration] = []
    for path in sorted(base.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if not match:
            continue
        found.append(Migration(version=int(match.group(1)), name=path.stem, path=path))
    return found


class MigrationRunner:
    def __init__(self, db: Database, clock: Clock, directory: Path | None = None) -> None:
        self._db = db
        self._clock = clock
        self._directory = directory
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )

    def current_version(self) -> int:
        row = self._db.query_one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
        return int(row["v"]) if row else 0

    def migrate(self) -> list[int]:
        """Apply pending migrations; returns the versions applied in this run."""
        applied_versions = {
            int(r["version"]) for r in self._db.query("SELECT version FROM schema_migrations")
        }
        applied_now: list[int] = []
        for migration in discover_migrations(self._directory):
            if migration.version in applied_versions:
                continue
            sql = migration.path.read_text(encoding="utf-8")
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            with self._db.transaction():
                for statement in statements:
                    self._db.execute(statement)
                self._db.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, _now_iso(self._clock)),
                )
            applied_now.append(migration.version)
        return applied_now

    def verify(self) -> None:
        """Raise if the database is missing tables expected by the latest migration."""
        version = self.current_version()
        expected = {m.version for m in discover_migrations(self._directory)}
        if version and version not in expected:
            raise ConfigurationError(
                f"Database schema version {version} is newer than the supported {max(expected)}",
                hint="Downgrading across schema versions is not supported.",
            )


def _now_iso(clock: Clock) -> str:
    return iso_utc(clock.now())
