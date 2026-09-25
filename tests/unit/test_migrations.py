import sqlite3
from pathlib import Path

import pytest

from rinari.shared.clock import FakeClock
from rinari.shared.errors import ConfigurationError
from rinari.storage.db import Database
from rinari.storage.migrations import MigrationRunner
from rinari.storage.migrations.runner import SUPERSEDED

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_0001 = REPO_ROOT / "src/rinari/storage/migrations/0001_initial.sql"

TABLES_AFTER_MIGRATIONS = {
    "schema_migrations",
    "skill_records",
    "scheduled_tasks",
    "scheduled_runs",
    "providers",
    "provider_credentials_metadata",
    "retained_credentials",
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
    "tasks",
    "validation_records",
    "checkpoints",
    "checkpoint_files",
    "artifacts",
    "user_memory",
    "project_memory",
    "episodic_memory",
    "pattern_memory",
    "memory_suppressions",
    "memory_sources",
    "memory_source_suppressions",
    "memory_conversation_controls",
    "memory_candidates",
    "memory_privacy_ledger",
    "memory_record_suppressions",
    "context_pins",
    "network_rules",
    "network_events",
    "plugins",
    "mcp_servers",
    "api_specs",
    "hooks",
    "turn_changesets",
    "turn_changed_files",
    "turn_change_undo_operations",
    "pending_credential_cleanup",
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
    assert applied == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        38,
        39,
        40,
    ]
    assert _table_names(db) == TABLES_AFTER_MIGRATIONS


def test_migrate_is_idempotent(db):
    runner = MigrationRunner(db, FakeClock())
    runner.migrate()
    assert runner.migrate() == []
    assert runner.current_version() == 40


def _previous_home_migrations(tmp_path: Path, upto: int) -> Path:
    """Bundled migrations 0001..upto, as an install that predates the rest."""
    custom = tmp_path / "migrations"
    custom.mkdir()
    for source in sorted(BUNDLED_0001.parent.glob("*.sql")):
        if int(source.name[:4]) <= upto:
            (custom / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return custom


def test_peer_origin_migration_keeps_previous_messages(db, tmp_path):
    """0032 runs over a home written before peer messaging existed: rows keep
    their content and read back with no origin; new rows round-trip theirs."""
    from rinari.storage.records import SessionMessageRecord
    from rinari.storage.repositories.sessions import SessionMessageRepository

    previous = _previous_home_migrations(tmp_path, upto=31)
    runner = MigrationRunner(db, FakeClock(), directory=previous)
    runner.migrate()
    assert runner.current_version() == 31
    columns = {row["name"] for row in db.query("PRAGMA table_info(session_messages)")}
    assert "origin_json" not in columns
    db.execute(
        "INSERT INTO sessions (id, kind, created_cwd, current_cwd, provider_id, model_id, "
        "state, created_at, updated_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ses_old", "CHAT", "/w", "/w", "prov", "mdl", "active", "t0", "t0", "t0"),
    )
    db.execute(
        "INSERT INTO session_messages (id, session_id, seq, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("msg_old", "ses_old", 1, "user", "antes de peers", "t0"),
    )

    applied = MigrationRunner(db, FakeClock()).migrate()
    # 0032 es la primera que le falta a ese home; las posteriores se aplican
    # detrás. Se calcula desde las migraciones incluidas para que añadir una
    # nueva no rompa una prueba cuyo objeto es 0032.
    after_31 = sorted(
        int(source.name[:4])
        for source in BUNDLED_0001.parent.glob("*.sql")
        if int(source.name[:4]) > 31
    )
    assert applied == after_31
    assert applied[0] == 32
    columns = {row["name"] for row in db.query("PRAGMA table_info(session_messages)")}
    assert "origin_json" in columns

    repo = SessionMessageRepository(db)
    old = repo.list("ses_old")
    assert [m.id for m in old] == ["msg_old"]
    assert old[0].content == "antes de peers"
    assert old[0].origin is None

    origin = {"kind": "peer", "source_session_id": "ses_b", "hop": 1}
    repo.append_many(
        "ses_old",
        [
            SessionMessageRecord(
                id="msg_new", session_id="ses_old", seq=0, role="user", origin=origin
            )
        ],
    )
    listed = repo.list("ses_old")
    assert [m.id for m in listed] == ["msg_old", "msg_new"]
    assert listed[0].origin is None
    assert listed[1].origin == origin


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


# -- migrations that two branches numbered alike ---------------------------------


def _home_with(tmp_path: Path, extra: dict[str, str]) -> Path:
    """0001..0034 plus other migrations, as another build would have shipped them."""
    custom = _previous_home_migrations(tmp_path, upto=34)
    for name, sql in extra.items():
        (custom / name).write_text(sql, encoding="utf-8")
    return custom


def test_a_home_migrated_by_the_voice_branch_gets_the_missing_migrations(db, tmp_path):
    """The owner's home had 0035_voice..0037_voice_context: the pins, skill
    records and schedules (now 0038..0040) must still be applied."""
    voice = _home_with(
        tmp_path,
        {
            "0035_voice.sql": "CREATE TABLE voice_probe (id TEXT)",
            "0036_turn_steering.sql": "CREATE TABLE steering_probe (id TEXT)",
            "0037_voice_context.sql": "CREATE TABLE voice_context_probe (id TEXT)",
        },
    )
    MigrationRunner(db, FakeClock(), directory=voice).migrate()
    assert MigrationRunner(db, FakeClock()).migrate() == [38, 39, 40]
    columns = {row["name"] for row in db.query("PRAGMA table_info(sessions)")}
    assert "pinned_at" in columns
    assert {"skill_records", "scheduled_tasks", "scheduled_runs"} <= _table_names(db)


def test_a_home_that_applied_the_old_numbers_is_not_migrated_twice(db, tmp_path):
    migrations = BUNDLED_0001.parent
    old = _home_with(
        tmp_path,
        {
            f"{old_name}.sql": (migrations / f"{new_name}.sql").read_text(encoding="utf-8")
            for new_name, old_name in SUPERSEDED.items()
        },
    )
    MigrationRunner(db, FakeClock(), directory=old).migrate()
    runner = MigrationRunner(db, FakeClock())
    assert runner.migrate() == []  # no duplicate pinned_at column, nothing re-run
    recorded = {row["version"]: row["name"] for row in db.query("SELECT * FROM schema_migrations")}
    assert recorded[38] == "0038_session_pins" and recorded[40] == "0040_scheduled_tasks"
    # The old numbers are free again for the migrations that own them.
    assert not {35, 36, 37} & set(recorded)


def test_a_version_applied_under_another_name_stops_with_a_clear_error(db, tmp_path):
    other = _home_with(tmp_path, {"0038_something_else.sql": "CREATE TABLE other_probe (id TEXT)"})
    MigrationRunner(db, FakeClock(), directory=other).migrate()
    with pytest.raises(ConfigurationError, match="0038_something_else"):
        MigrationRunner(db, FakeClock()).migrate()
