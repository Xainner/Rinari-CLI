"""Versioned, transactional SQL migrations.

Migrations are numbered .sql files (NNNN_name.sql) applied in order.
Each migration runs in a single transaction; a failure rolls back all
of its statements and leaves the schema_migrations record untouched.

A version is identified by its number *and* its name. Two branches once
shipped different 0035-0037 migrations; a database migrated by one of them
then skipped the other's by number and ran with a broken schema. Now a
recorded version whose name differs from this build's file of the same
number stops the Engine with a clear error instead of passing silently, and
migrations that had to move keep their old name in `SUPERSEDED` so a
database that already applied them under the old number is not migrated
twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rinari.shared.clock import Clock, iso_utc
from rinari.shared.errors import ConfigurationError
from rinari.storage.db import Database

_MIGRATION_FILE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")

# New name -> the name it was applied under before being renumbered. They
# collided with 0035_voice, 0036_turn_steering and 0037_voice_context.
SUPERSEDED: dict[str, str] = {
    "0038_session_pins": "0035_session_pins",
    "0039_skill_records": "0036_skill_records",
    "0040_scheduled_tasks": "0037_scheduled_tasks",
}
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

    def _recorded(self) -> dict[int, str]:
        rows = self._db.query("SELECT version, name FROM schema_migrations")
        return {int(row["version"]): str(row["name"]) for row in rows}

    def _adopt_superseded(self, migrations: list[Migration]) -> None:
        """A renumbered migration applied under its old number counts as
        applied under the new one; the old row is dropped so that number is
        free for whatever migration really owns it."""
        recorded = self._recorded()
        by_name = {name: version for version, name in recorded.items()}
        for migration in migrations:
            old = SUPERSEDED.get(migration.name)
            if old is None or old not in by_name or migration.version in recorded:
                continue
            with self._db.transaction():
                self._db.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, _now_iso(self._clock)),
                )
                self._db.execute(
                    "DELETE FROM schema_migrations WHERE version = ? AND name = ?",
                    (by_name[old], old),
                )

    def migrate(self) -> list[int]:
        """Apply pending migrations; returns the versions applied in this run."""
        migrations = discover_migrations(self._directory)
        self._adopt_superseded(migrations)
        recorded = self._recorded()
        applied_now: list[int] = []
        for migration in migrations:
            if migration.version in recorded:
                if recorded[migration.version] != migration.name:
                    raise ConfigurationError(
                        f"Database migration {migration.version} was applied as "
                        f"{recorded[migration.version]!r}, but this build ships "
                        f"{migration.name!r}.",
                        hint=(
                            "The state database was migrated by a different build of Rinari "
                            "(another branch). Use the build that migrated it, or restore a "
                            "backup of ~/.rinari/state.db."
                        ),
                    )
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
