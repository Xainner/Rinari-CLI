import sqlite3

import pytest

from rinari.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    yield database
    database.close()


def test_wal_mode_enabled(db):
    row = db.query_one("PRAGMA journal_mode")
    assert row["journal_mode"].lower() == "wal"


def test_foreign_keys_enforced(db):
    db.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id))")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO child (id, parent_id) VALUES ('c1', 'missing')")
    db.execute("INSERT INTO parent (id) VALUES ('p1')")
    db.execute("INSERT INTO child (id, parent_id) VALUES ('c1', 'p1')")


def test_transaction_commits_on_success(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    with db.transaction():
        db.execute("INSERT INTO t (v) VALUES ('a')")
        db.execute("INSERT INTO t (v) VALUES ('b')")
    assert len(db.query("SELECT * FROM t")) == 2


def test_transaction_rolls_back_on_error(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("INSERT INTO t (v) VALUES ('a')")
        raise RuntimeError("boom")
    assert db.query("SELECT * FROM t") == []


def test_state_file_created(db):
    db.execute("CREATE TABLE t (id INTEGER)")
    assert db.path.exists()


def test_close_waits_for_a_statement_running_in_another_thread(tmp_path) -> None:
    import sqlite3
    import threading
    import time

    db = Database(tmp_path / "race.db")
    db.execute("CREATE TABLE t (x INTEGER)")
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            try:
                db.execute("INSERT INTO t VALUES (1)")
            except sqlite3.ProgrammingError:
                return  # closed: a clean error, never a crash
            except BaseException as exc:  # pragma: no cover - the failure being tested
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    db.close()
    stop.set()
    for thread in threads:
        thread.join(5)
    assert not errors
    try:
        db.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a closed database must refuse statements")
