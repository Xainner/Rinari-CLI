import pytest

from rinari.artifacts.store import ArtifactStore, ArtifactURIError, build_uri, parse_uri
from rinari.shared.errors import NotFoundError


def test_uri_roundtrip():
    uri = build_uri("ses_1", "tests", "pytest.log")
    assert parse_uri(uri) == ("ses_1", "tests", "pytest.log")
    assert uri == "artifact://ses_1/tests/pytest.log"


def test_uri_rejects_bad_shapes():
    with pytest.raises(ArtifactURIError):
        parse_uri("http://x/y/z")
    with pytest.raises(ArtifactURIError):
        parse_uri("artifact://only/session")
    with pytest.raises(ArtifactURIError):
        parse_uri("artifact://ses/with/slashes/extra/name")
    with pytest.raises(ArtifactURIError):
        parse_uri("artifact://ses/../etc/passwd")


def test_create_and_get(app_ctx):
    store = ArtifactStore(app_ctx)
    record = store.create(
        "ses_1",
        "tests",
        "pytest.log",
        b"1 passed\n",
        summary="unit run",
        provenance="shell:pytest",
        project_root="/repo",
    )
    assert record.uri() == "artifact://ses_1/tests/pytest.log"
    assert store.get(record.uri()) == b"1 passed\n"
    assert store.meta(record.uri()).sha256 == record.sha256

    # overwrite same coordinate (upsert)
    record2 = store.create("ses_1", "tests", "pytest.log", b"2 passed\n")
    assert store.get(record2.uri()) == b"2 passed\n"


def test_list_and_search(app_ctx):
    store = ArtifactStore(app_ctx)
    store.create("ses_1", "tests", "a.log", b"segmentation fault in worker\n")
    store.create("ses_1", "notes", "b.md", b"design notes", summary="design")
    items = store.list(session_id="ses_1")
    assert {r.name for r in items} == {"a.log", "b.md"}

    hits = store.search("segmentation")
    assert [h["name"] for h in hits] == ["a.log"]
    assert hits[0]["matched"] == "content"

    hits = store.search("design")
    assert [h["name"] for h in hits] == ["b.md"]
    assert hits[0]["matched"] == "metadata"

    assert store.list(project_root="/nope") == []


def test_lines_and_export(app_ctx, tmp_path):
    store = ArtifactStore(app_ctx)
    record = store.create("ses_1", "data", "rows.csv", b"1\n2\n3\n4\n")
    lines, truncated = store.lines(record.uri(), 1, 3)
    assert lines == ["2", "3"]
    assert truncated is True

    dest = store.export(record.uri(), tmp_path / "out.csv")
    assert dest.read_bytes() == b"1\n2\n3\n4\n"


def test_remove_and_gc(app_ctx):
    store = ArtifactStore(app_ctx)
    live = store.create("ses_live", "x", "a.txt", b"a")
    store.create("ses_gone", "x", "b.txt", b"b")
    # keep ses_live as an active session row
    from rinari.storage.records import SessionRecord

    now = "2026-01-01T00:00:00Z"
    app_ctx.session_repo.insert(
        SessionRecord(
            id="ses_live",
            kind="CHAT",
            title="t",
            project_id=None,
            project_root_snapshot=None,
            created_cwd="/tmp",
            current_cwd="/tmp",
            provider_id="",
            model_id="",
            profile_id="",
            mode="default",
            state="active",
            compact_state=None,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )
    # gc keeps the live session's artifact, drops the gone one
    assert store.gc() == 1
    assert store.meta(live.uri()).byte_count == 1
    with pytest.raises(NotFoundError):
        store.meta("artifact://ses_gone/x/b.txt")

    assert store.remove(live.uri()) is True
    assert store.remove(live.uri()) is False


def test_long_artifact_paths_are_readable_and_writable(app_ctx, tmp_path):
    """Derived names carry full digests; the store must survive legacy MAX_PATH.

    Regression for Windows CI: a 240+ character absolute path made every
    ``open()`` fail with ENOENT although the directory existed.
    """
    from rinari.artifacts.store import os_path
    from rinari.artifacts.transfer import import_file

    store = ArtifactStore(app_ctx)
    name = "vision-" + "a" * 64 + "-" + "b" * 64 + "-" + "c" * 32 + ".txt"
    assert len(str(store._root() / "ses_long" / "derived" / name)) > 240
    record = store.create_text("ses_long", "derived", name, "observed", provenance="test")
    assert store.get(record.uri()) == b"observed"
    assert store.read_text(record.uri()) == ("observed", False)

    source = tmp_path / ("s" * 90 + ".txt")
    source.write_text("imported", encoding="utf-8")
    imported = import_file(store, "ses_long", source)
    assert store.get(imported.uri()) == b"imported"

    assert store.remove(record.uri()) is True
    assert not os_path(store._root() / "ses_long" / "derived" / name).exists()
