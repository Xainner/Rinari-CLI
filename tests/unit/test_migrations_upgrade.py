"""Schema upgrade tests (Fase 8 hardening): a database migrated to an older
version must upgrade to the latest in place, preserving user data
(providers, models, sessions, events) per AGENTS.md section 35.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rinari.shared.clock import FakeClock
from rinari.shared.errors import ConfigurationError
from rinari.storage.db import Database
from rinari.storage.migrations.runner import MigrationRunner, discover_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "rinari" / "storage" / "migrations"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


def _clock() -> FakeClock:
    return FakeClock(start=1_700_000_000.0, step=1.0)


def _stage_migrations(tmp_path: Path, cutoff: int) -> Path:
    staged = tmp_path / f"migrations_upto_{cutoff}"
    staged.mkdir()
    for migration in discover_migrations():
        if migration.version <= cutoff:
            shutil.copy(migration.path, staged / migration.path.name)
    return staged


def test_upgrade_preserves_data(db_path, tmp_path):
    """Migrate to 0004, seed data, then upgrade to latest: data survives."""
    clock = _clock()
    cutoff = 4  # 0001..0004 (providers, models, sessions, events, trust)
    staged = _stage_migrations(tmp_path, cutoff)

    db = Database(db_path)
    MigrationRunner(db, clock, directory=staged).migrate()
    assert MigrationRunner(db, clock, directory=staged).current_version() == cutoff

    now = "2024-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO providers (id, alias, type, auth_method, created_at, updated_at) "
        "VALUES ('prov_1', 'openai-fake', 'openai', 'api-key', ?, ?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO models (id, alias, provider_id, provider_model_id, created_at, updated_at) "
        "VALUES ('mdl_1', 'gpt-fake', 'prov_1', 'gpt-fake-1', ?, ?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO sessions (id, kind, created_cwd, current_cwd, provider_id, model_id, "
        "created_at, updated_at, last_active_at) VALUES ('ses_1', 'CHAT', '/tmp', '/tmp', "
        "'prov_1', 'mdl_1', ?, ?, ?)",
        (now, now, now),
    )
    db.execute(
        "INSERT INTO session_events (id, session_id, seq, type, created_at) "
        "VALUES ('evt_1', 'ses_1', 1, 'SessionStarted', ?)",
        (now,),
    )
    db.close()

    # Upgrade across all remaining migrations.
    db2 = Database(db_path)
    latest = max(m.version for m in discover_migrations())
    runner2 = MigrationRunner(db2, clock)
    applied = runner2.migrate()
    assert applied == list(range(cutoff + 1, latest + 1))
    assert runner2.current_version() == latest
    runner2.verify()

    provider = db2.query_one("SELECT alias FROM providers WHERE id = 'prov_1'")
    assert provider["alias"] == "openai-fake"
    model = db2.query_one("SELECT alias FROM models WHERE id = 'mdl_1'")
    assert model["alias"] == "gpt-fake"
    session = db2.query_one("SELECT kind, provider_id FROM sessions WHERE id = 'ses_1'")
    assert session["kind"] == "CHAT"
    assert session["provider_id"] == "prov_1"
    event = db2.query_one("SELECT type FROM session_events WHERE id = 'evt_1'")
    assert event["type"] == "SessionStarted"
    db2.close()


def test_upgrade_idempotent(db_path):
    """Migrating twice applies nothing the second time."""
    clock = _clock()
    db = Database(db_path)
    first = MigrationRunner(db, clock).migrate()
    assert len(first) == len(discover_migrations())
    assert MigrationRunner(db, clock).migrate() == []
    db.close()


def test_schema_version_table_tracks_names(db_path):
    clock = _clock()
    db = Database(db_path)
    MigrationRunner(db, clock).migrate()
    rows = db.query("SELECT version, name FROM schema_migrations ORDER BY version")
    migrations = sorted(discover_migrations(), key=lambda m: m.version)
    assert [r["version"] for r in rows] == [m.version for m in migrations]
    assert [r["name"] for r in rows] == [m.name for m in migrations]
    db.close()


def test_newer_schema_than_supported_rejected(db_path, tmp_path):
    """A database migrated by a future version is rejected on verify()."""
    clock = _clock()
    future = tmp_path / "migrations_future"
    future.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", future / "0001_initial.sql")
    (future / "9999_future.sql").write_text(
        "CREATE TABLE future_table (id TEXT PRIMARY KEY)", encoding="utf-8"
    )
    db = Database(db_path)
    MigrationRunner(db, clock, directory=future).migrate()
    db.close()

    db2 = Database(db_path)
    with pytest.raises(ConfigurationError):
        MigrationRunner(db2, clock).verify()
    db2.close()
