import sqlite3
from collections.abc import Iterator

import pytest

from rinari.shared.clock import FakeClock
from rinari.shared.ids import IdGenerator
from rinari.storage.db import Database
from rinari.storage.migrations import MigrationRunner
from rinari.storage.records import (
    ConfigValue,
    ModelRecord,
    ProjectRecord,
    ProviderCredentialRef,
    ProviderRecord,
    SessionEventRecord,
    SessionRecord,
)
from rinari.storage.repositories import (
    ConfigValueRepository,
    ModelRepository,
    ProjectRepository,
    ProviderRepository,
    SessionEventRepository,
    SessionRepository,
)

CLOCK = FakeClock(start=1_700_000_000.0, step=1.0)
NOW = "2023-11-14T22:13:20.000Z"


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database(tmp_path / "state.db")
    MigrationRunner(database, CLOCK).migrate()
    yield database
    database.close()


def _provider(alias="openai-personal", **kwargs) -> ProviderRecord:
    base = dict(
        id="prov_01",
        type="openai",
        auth_method="api-key",
        account_hint=None,
        endpoint=None,
        settings={},
        default_model_id=None,
        last_used_model_id=None,
        status_connected=None,
        status_checked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kwargs)
    return ProviderRecord(alias=alias, **base)


def _model(provider_id="prov_01", alias="main", **kwargs) -> ModelRecord:
    base = dict(
        id="mdl_01",
        provider_model_id="gpt-test",
        settings={},
        capabilities=None,
        availability="unknown",
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kwargs)
    return ModelRecord(alias=alias, provider_id=provider_id, **base)


def test_provider_roundtrip(db):
    repo = ProviderRepository(db)
    rec = _provider(settings={"temperature": 0.2})
    repo.insert(rec)
    loaded = repo.get("prov_01")
    assert loaded == rec
    assert loaded.settings == {"temperature": 0.2}
    assert repo.get_by_alias("openai-personal") is not None


def test_provider_alias_unique(db):
    repo = ProviderRepository(db)
    repo.insert(_provider(alias="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert(_provider(alias="dup", id="prov_02"))


def test_provider_credentials_are_stored_as_references_only(db):
    # Invariant: secret values never persist in provider config rows.
    repo = ProviderRepository(db)
    repo.insert(_provider())
    repo.set_credential(
        ProviderCredentialRef(
            provider_id="prov_01",
            secret_ref="env://OPENAI_API_KEY",
            method="api-key",
            updated_at=NOW,
        )
    )
    ref = repo.get_credential("prov_01")
    assert ref.secret_ref == "env://OPENAI_API_KEY"
    row = db.query_one("SELECT settings_json FROM providers WHERE id = 'prov_01'")
    assert row["settings_json"] == "{}"


def test_provider_update_preserves_id(db):
    repo = ProviderRepository(db)
    repo.insert(_provider(alias="old-name"))
    renamed = _provider(alias="new-name", id="prov_01")
    repo.update(renamed)
    assert repo.get("prov_01").alias == "new-name"
    assert repo.get_by_alias("old-name") is None


def test_model_roundtrip_and_scoping(db):
    providers = ProviderRepository(db)
    providers.insert(_provider(id="prov_01", alias="a"))
    providers.insert(_provider(id="prov_02", alias="b"))
    models = ModelRepository(db)
    models.insert(_model(provider_id="prov_01", alias="main"))
    # Different providers may both alias "main".
    models.insert(_model(provider_id="prov_02", alias="main", id="mdl_02"))
    with pytest.raises(sqlite3.IntegrityError):
        models.insert(_model(provider_id="prov_01", alias="main", id="mdl_03"))

    assert len(models.list(provider_id="prov_01")) == 1
    assert len(models.list()) == 2
    assert models.get_by_alias("prov_02", "main") is not None


def test_project_roundtrip(db):
    repo = ProjectRepository(db)
    rec = ProjectRecord(
        id="prj_01",
        canonical_root="/home/user/app",
        git_fingerprint="abc123",
        metadata={"trust": "untrusted"},
        created_at=NOW,
        updated_at=NOW,
    )
    repo.insert(rec)
    assert repo.get("prj_01") == rec
    assert repo.get_by_root("/home/user/app").id == "prj_01"
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert(
            ProjectRecord(
                id="prj_02",
                canonical_root="/home/user/app",
                git_fingerprint=None,
                metadata={},
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_session_roundtrip_and_listing(db):
    sessions = SessionRepository(db)
    rec = SessionRecord(
        id="ses_01",
        kind="CHAT",
        title=None,
        project_id=None,
        project_root_snapshot=None,
        created_cwd="/home/user",
        current_cwd="/home/user",
        provider_id="prov_01",
        model_id="mdl_01",
        profile_id="default",
        mode="ask",
        state="active",
        compact_state=None,
        created_at=NOW,
        updated_at=NOW,
        last_active_at=NOW,
    )
    sessions.insert(rec)
    assert sessions.get("ses_01") == rec
    assert [s.id for s in sessions.list(kind="CHAT")] == ["ses_01"]
    assert sessions.list(kind="PROJECT") == []


def test_session_events_append_and_order(db):
    sessions = SessionRepository(db)
    sessions.insert(
        SessionRecord(
            id="ses_01",
            kind="CHAT",
            title=None,
            project_id=None,
            project_root_snapshot=None,
            created_cwd="/x",
            current_cwd="/x",
            provider_id="prov_01",
            model_id="mdl_01",
            profile_id="default",
            mode="ask",
            state="active",
            compact_state=None,
            created_at=NOW,
            updated_at=NOW,
            last_active_at=NOW,
        )
    )
    events = SessionEventRepository(db)
    ids = IdGenerator(CLOCK)
    for i, etype in enumerate(["SessionCreated", "UserMessage", "SessionCreated"]):
        events.insert(
            SessionEventRecord(
                id=ids.new("evt"),
                session_id="ses_01",
                seq=events.next_seq("ses_01"),
                type=etype,
                payload={"n": i},
                created_at=NOW,
            )
        )
    listed = events.list("ses_01")
    assert [e.seq for e in listed] == [1, 2, 3]
    assert listed[1].payload == {"n": 1}
    assert len(events.list("ses_01", after_seq=1)) == 2


def test_session_project_fk_enforced(db):
    sessions = SessionRepository(db)
    with pytest.raises(sqlite3.IntegrityError):
        sessions.insert(
            SessionRecord(
                id="ses_01",
                kind="PROJECT",
                title=None,
                project_id="prj_missing",
                project_root_snapshot="/x",
                created_cwd="/x",
                current_cwd="/x",
                provider_id="prov_01",
                model_id="mdl_01",
                profile_id="default",
                mode="agent",
                state="active",
                compact_state=None,
                created_at=NOW,
                updated_at=NOW,
                last_active_at=NOW,
            )
        )


def test_config_values_roundtrip(db):
    repo = ConfigValueRepository(db)
    repo.set(ConfigValue(key="provider.active", value="prov_01", updated_at=NOW))
    assert repo.get("provider.active") == "prov_01"
    repo.set(ConfigValue(key="provider.active", value="prov_02", updated_at=NOW))
    assert repo.get("provider.active") == "prov_02"
    assert repo.delete("provider.active") is True
    assert repo.get("provider.active") is None


def test_config_values_prefix_query_escapes_specials(db):
    repo = ConfigValueRepository(db)
    for key in ("provider.active", "provider%weird", "profile.x"):
        repo.set(ConfigValue(key=key, value="1", updated_at=NOW))
    assert list(repo.keys_with_prefix("provider")) == ["provider%weird", "provider.active"]
