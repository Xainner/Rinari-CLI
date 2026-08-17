import sqlite3
from pathlib import Path

import pytest

from rinari.shared.clock import FakeClock
from rinari.shared.errors import ConfigurationError
from rinari.storage.db import Database
from rinari.storage.migrations import MigrationRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_0001 = REPO_ROOT / "src/rinari/storage/migrations/0001_initial.sql"

TABLES_AFTER_MIGRATIONS = {
    "schema_migrations",
    "providers",
    "provider_credentials_metadata",
    "models",
    "projects",
    "sessions",
    "session_events",
    "session_messages",
    "worktree_baselines",
    "trust_entries",
    "config_values",
    "repo_index_files",
    "repo_index_symbols",
    "repo_index_references",
    "repo_index_test_map",
    "repo_index_meta",
}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    yield database
    database.close()


def _table_names(db: Database) -> set[str]:
    rows = db.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row["name"] for row in rows}


def test_migrate_fresh_database_applies_all(db):
    applied = MigrationRunner(db, FakeClock()).migrate()
    assert applied == [1, 2, 3, 4, 5]
    assert _table_names(db) == TABLES_AFTER_MIGRATIONS


def test_migrate_is_idempotent(db):
    runner = MigrationRunner(db, FakeClock())
    runner.migrate()
    assert runner.migrate() == []
    assert runner.current_version() == 5


def test_migrations_are_replayed_from_on_disk(db, tmp_path):
    # Simulate an install that only saw 0001, then a code update exposing 0002.
    custom = tmp_path / "migrations"
    custom.mkdir()
    (custom / "0001_initial.sql").write_text(
        BUNDLED_0001.read_text(encoding="utf-8"), encoding="utf-8"
    )
    MigrationRunner(db, FakeClock(), directory=custom).migrate()

    (custom / "0002_add_note.sql").write_text(
        "CREATE TABLE note_test (id TEXT PRIMARY KEY)", encoding="utf-8"
    )
    applied = MigrationRunner(db, FakeClock(), directory=custom).migrate()
    assert applied == [2]
    assert "note_test" in _table_names(db)


def test_failed_migration_rolls_back_atomically(db, tmp_path):
    custom = tmp_path / "migrations"
    custom.mkdir()
    (custom / "0001_initial.sql").write_text(
        BUNDLED_0001.read_text(encoding="utf-8"), encoding="utf-8"
    )

    (custom / "0002_bad.sql").write_text(
        "CREATE TABLE half_baked (id TEXT PRIMARY KEY);\nCREATE TABLE broken (no_close_paren;",
        encoding="utf-8",
    )
    runner = MigrationRunner(db, FakeClock(), directory=custom)

    with pytest.raises(sqlite3.OperationalError):
        runner.migrate()

    assert "half_baked" not in _table_names(db)
    assert "broken" not in _table_names(db)
    assert runner.current_version() == 1


def test_verify_raises_when_db_version_not_in_supported_set(db, tmp_path):
    custom = tmp_path / "migrations"
    custom.mkdir()
    (custom / "0001_initial.sql").write_text(
        "CREATE TABLE old_probe (id TEXT PRIMARY KEY)", encoding="utf-8"
    )
    runner = MigrationRunner(db, FakeClock(), directory=custom)
    runner.migrate()
    # Database now claims version 1, but a "newer" migration set only has 0002+
    custom2 = tmp_path / "migrations2"
    custom2.mkdir()
    (custom2 / "0002_only.sql").write_text(
        "CREATE TABLE newer (id TEXT PRIMARY KEY)", encoding="utf-8"
    )
    runner2 = MigrationRunner(db, FakeClock(), directory=custom2)
    # Version 1 is not in the newer set: verify() must object.
    with pytest.raises(ConfigurationError):
        runner2.verify()
