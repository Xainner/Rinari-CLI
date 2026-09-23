from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.desktop import PREVIEW_LIMIT, FileWatchRegistry, _fingerprint
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.server import EngineServer
from rinari.storage.records import SessionRecord


@pytest.fixture
def desktop_runtime(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user_home.mkdir()
    workspace.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    provider = services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="not-a-real-secret",
        )
    )
    model = services.models.add("fake", "fake-model", "fake-model")
    now = "2026-01-01T00:00:00Z"

    def session(identity: str, root: Path = workspace) -> SessionRecord:
        record = SessionRecord(
            id=identity,
            kind="CHAT",
            title=identity,
            project_id=None,
            project_root_snapshot=None,
            created_cwd=str(root),
            current_cwd=str(root),
            provider_id=provider.id,
            model_id=model.id,
            profile_id="default",
            mode="build",
            state="active",
            compact_state=None,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            permission_profile="full-access",
        )
        app_ctx.session_repo.insert(record)
        return record

    primary = session("ses_primary")
    other = session("ses_other")
    server = EngineServer(services, user_home=user_home)
    yield services, server, primary, other, workspace, tmp_path
    server.close()


def _changeset(services, session_id: str, turn_id: str, path: Path, **overrides) -> None:
    data = path.read_bytes() if path.exists() else b""
    row = {
        "path": str(path),
        "absolute_path": str(path.resolve()),
        "previous_path": None,
        "kind": "created",
        "additions": 1,
        "deletions": 0,
        "before_exists": False,
        "after_exists": True,
        "before_hash": None,
        "after_hash": hashlib.sha256(data).hexdigest(),
        "before_size": None,
        "after_size": len(data),
        "ownership": "agent",
        "confidence": "exact",
        "binary": False,
        "sensitive": False,
        "diff": "+content",
        "diff_truncated": False,
        "undoable": True,
        "conflict_reason": None,
        "before_blob_ref": None,
        **overrides,
    }
    services.ctx.turn_change_repo.insert(
        {
            "id": f"chg_{turn_id}",
            "turn_id": turn_id,
            "session_id": session_id,
            "project_id": None,
            "roots": [],
            "created_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "additions": 1,
            "deletions": 0,
            "undoable": True,
            "attribution_complete": False,
            "warnings": ["partial"],
            "status": "completed",
        },
        [row],
    )


_REQUEST_IDS = itertools.count()


def _request(server: EngineServer, method: str, params: dict):
    return server.handle_line(
        json.dumps({"id": f"request-{next(_REQUEST_IDS)}", "method": method, "params": params})
    )


def test_workspace_file_read_preserves_workspace_behavior(desktop_runtime) -> None:
    _services, server, primary, _other, workspace, _tmp = desktop_runtime
    target = workspace / "inside.txt"
    target.write_bytes(b"inside\n")
    result = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target)},
    )
    assert result["ok"] is True
    assert result["result"]["content"] == "inside\n"
    assert result["result"]["provenance"] == "workspace"


def test_exact_external_changeset_authorizes_current_sensitive_content(desktop_runtime) -> None:
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    target = tmp_path / "external" / ".env"
    target.parent.mkdir()
    target.write_bytes(b"SECRET=one\n")
    _changeset(services, primary.id, "turn_external", target, sensitive=True, confidence="observed")

    first = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target), "turn_id": "turn_external"},
    )
    assert first["ok"] is True
    assert first["result"]["sensitive"] is True
    assert first["result"]["changed_since_turn"] is False

    target.write_bytes(b"SECRET=two\n")
    current = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target), "turn_id": "turn_external"},
    )
    assert current["result"]["content"] == "SECRET=two\n"
    assert current["result"]["changed_since_turn"] is True


def test_external_provenance_does_not_expand_authority(desktop_runtime) -> None:
    services, server, primary, other, _workspace, tmp_path = desktop_runtime
    allowed = tmp_path / "external" / "allowed.txt"
    allowed.parent.mkdir()
    allowed.write_text("allowed", encoding="utf-8")
    denied = allowed.with_name("allowed.txt.extra")
    denied.write_text("denied", encoding="utf-8")
    _changeset(services, primary.id, "turn_exact", allowed)

    wrong_path = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(denied), "turn_id": "turn_exact"},
    )
    assert wrong_path["error"]["code"] == "PERMISSION_DENIED"
    wrong_session = _request(
        server,
        "workspace.file.read",
        {"session_id": other.id, "path": str(allowed), "turn_id": "turn_exact"},
    )
    assert wrong_session["error"]["code"] == "PERMISSION_DENIED"

    no_turn = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(allowed)},
    )
    assert no_turn["error"]["code"] == "PERMISSION_DENIED"
    unknown_turn = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(allowed), "turn_id": "turn_other"},
    )
    assert unknown_turn["error"]["code"] == "INVALID_PARAMS"


@pytest.mark.parametrize("kind", ["created", "modified", "renamed"])
def test_all_persisted_after_states_authorize_the_exact_unicode_path(
    desktop_runtime, kind: str
) -> None:
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    target = tmp_path / "afuera" / "café 日本語.txt"
    target.parent.mkdir()
    target.write_text(kind, encoding="utf-8")
    _changeset(services, primary.id, f"turn_{kind}", target, kind=kind)
    result = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target), "turn_id": f"turn_{kind}"},
    )
    assert result["ok"] is True
    assert result["result"]["content"] == kind


def test_deleted_rows_never_authorize_a_recreated_external_file(desktop_runtime) -> None:
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    target = tmp_path / "outside" / "deleted.txt"
    target.parent.mkdir()
    target.write_text("new unrelated content", encoding="utf-8")
    _changeset(
        services,
        primary.id,
        "turn_deleted",
        target,
        kind="deleted",
        after_exists=False,
        after_hash=None,
    )
    result = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target), "turn_id": "turn_deleted"},
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"


def test_deleted_and_previous_rename_paths_are_not_authorized(desktop_runtime) -> None:
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    old = tmp_path / "outside" / "old.txt"
    new = old.with_name("new.txt")
    old.parent.mkdir()
    new.write_text("renamed", encoding="utf-8")
    _changeset(
        services,
        primary.id,
        "turn_rename",
        new,
        kind="renamed",
        previous_path=str(old.resolve()),
    )
    old.write_text("not the turn output", encoding="utf-8")

    good = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(new), "turn_id": "turn_rename"},
    )
    assert good["ok"] is True
    bad = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(old), "turn_id": "turn_rename"},
    )
    assert bad["error"]["code"] == "PERMISSION_DENIED"


def test_engine_home_remains_private_even_with_changeset(desktop_runtime) -> None:
    services, server, primary, _other, _workspace, _tmp = desktop_runtime
    target = services.ctx.layout.root / "credentials" / "secret.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")
    _changeset(services, primary.id, "turn_private", target, sensitive=True)
    denied = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(target), "turn_id": "turn_private"},
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"


def test_replacing_authorized_file_with_symlink_does_not_transfer_authority(
    desktop_runtime,
) -> None:
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    allowed = tmp_path / "outside" / "allowed.txt"
    secret = tmp_path / "outside" / "secret.txt"
    allowed.parent.mkdir()
    allowed.write_text("allowed", encoding="utf-8")
    secret.write_text("secret", encoding="utf-8")
    _changeset(services, primary.id, "turn_symlink", allowed)
    allowed.unlink()
    try:
        allowed.symlink_to(secret)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this Windows host")

    denied = _request(
        server,
        "workspace.file.read",
        {"session_id": primary.id, "path": str(allowed), "turn_id": "turn_symlink"},
    )
    assert denied["error"]["code"] == "PERMISSION_DENIED"


def test_file_watch_events_are_bounded_and_content_free(tmp_path) -> None:
    events = []
    target = tmp_path / "watched.txt"
    target.write_text("one", encoding="utf-8")
    watches = FileWatchRegistry(events.append, max_watches=1, auto_start=False)
    watch_id = watches.add("session", target, "turn")
    with pytest.raises(EngineProtocolError) as limit:
        watches.add("session", target, "turn")
    assert limit.value.code == "FILE_WATCH_LIMIT"

    target.write_text("two", encoding="utf-8")
    watches.scan_once()
    target.unlink()
    watches.scan_once()
    target.write_text("three", encoding="utf-8")
    watches.scan_once()

    payloads = [item["payload"] for item in events]
    assert [item["state"] for item in payloads] == ["changed", "deleted", "recreated"]
    assert [item["revision"] for item in payloads] == [1, 2, 3]
    assert all(item["watch_id"] == watch_id for item in payloads)
    assert all("content" not in item for item in payloads)
    assert watches.remove(watch_id) is True
    watches.close()


def test_file_watch_detects_replacement_by_symlink(tmp_path) -> None:
    events = []
    watched = tmp_path / "watched.txt"
    target = tmp_path / "other.txt"
    watched.write_text("one", encoding="utf-8")
    target.write_text("other", encoding="utf-8")
    watches = FileWatchRegistry(events.append, auto_start=False)
    watches.add("session", watched, "turn")
    watched.unlink()
    try:
        watched.symlink_to(target)
    except OSError:
        watches.close()
        pytest.skip("Creating symlinks is unavailable on this Windows host")
    watches.scan_once()
    assert events[0]["payload"]["state"] == "changed"
    watches.close()


def test_file_watch_protocol_invalidates_and_unwatches(desktop_runtime) -> None:
    _services, server, primary, _other, workspace, _tmp = desktop_runtime
    target = workspace / "live.txt"
    target.write_text("one", encoding="utf-8")
    watched = _request(
        server,
        "workspace.file.watch",
        {"session_id": primary.id, "path": str(target)},
    )
    assert watched["ok"] is True
    watch_id = watched["result"]["watch_id"]
    assert watched["result"]["preview"]["content"] == "one"

    target.write_text("two", encoding="utf-8")
    server._desktop._watches.scan_once()
    changed = [row for row in server.drain_events() if row["event"] == "workspace.file.changed"]
    assert changed == [
        {
            "type": "event",
            "event": "workspace.file.changed",
            "payload": {
                "watch_id": watch_id,
                "session_id": primary.id,
                "path": str(target.resolve()),
                "revision": 1,
                "state": "changed",
            },
        }
    ]
    removed = _request(
        server, "workspace.file.unwatch", {"session_id": primary.id, "watch_id": watch_id}
    )
    assert removed["result"] == {}


def test_closing_a_session_releases_its_watches(desktop_runtime) -> None:
    _services, server, primary, _other, workspace, _tmp = desktop_runtime
    target = workspace / "close.txt"
    target.write_text("one", encoding="utf-8")
    watched = _request(
        server,
        "workspace.file.watch",
        {"session_id": primary.id, "path": str(target)},
    )
    assert watched["ok"] is True
    closed = _request(server, "session.close", {"ref": primary.id})
    assert closed["ok"] is True
    target.write_text("two", encoding="utf-8")
    server._desktop._watches.scan_once()
    assert not [row for row in server.drain_events() if row["event"] == "workspace.file.changed"]


def test_external_html_does_not_authorize_serving_its_siblings(desktop_runtime) -> None:
    services, server, primary, _other, _workspace, tmp = desktop_runtime
    page = tmp / "external.html"
    page.write_text("<h1>Allowed text</h1>", encoding="utf-8")
    _changeset(services, primary.id, "html", page)
    params = {"session_id": primary.id, "path": str(page), "turn_id": "html"}
    assert _request(server, "workspace.file.read", params)["ok"]
    assert (
        _request(server, "workspace.preview.start", params)["error"]["code"] == "PERMISSION_DENIED"
    )


@pytest.mark.parametrize(
    "content,code",
    [(b"\0binary", "UNSUPPORTED_FILE"), (b"x" * (PREVIEW_LIMIT + 1), "FILE_TOO_LARGE")],
    ids=["binary", "oversized"],
)
def test_watch_rejects_invalid_preview_without_leaking_registration(desktop_runtime, content, code):
    _services, server, primary, _other, workspace, _tmp = desktop_runtime
    target = workspace / "invalid.txt"
    target.write_bytes(content)
    result = _request(
        server, "workspace.file.watch", {"session_id": primary.id, "path": str(target)}
    )
    assert result["error"]["code"] == code
    assert not server._desktop._watches._items


def test_fingerprint_never_reads_a_changed_canonical_target(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed.txt"
    allowed.write_text("original", encoding="utf-8")
    forbidden = tmp_path / "forbidden.txt"
    monkeypatch.setattr(Path, "resolve", lambda self, **_kw: forbidden)
    monkeypatch.setattr(os, "open", lambda *_a, **_kw: pytest.fail("read unauthorized target"))
    assert _fingerprint(allowed, allowed) == ("target_changed",)


def test_fingerprint_caps_reads_even_when_file_grows_after_stat(tmp_path, monkeypatch):
    target = tmp_path / "growing.txt"
    target.write_bytes(b"x")
    original = os.open

    def grow_then_open(path, flags):
        target.write_bytes(b"y" * (PREVIEW_LIMIT + 10))
        return original(path, flags)

    monkeypatch.setattr(os, "open", grow_then_open)
    result = _fingerprint(target, target.resolve())
    assert result[-1] == hashlib.sha256(b"y" * (PREVIEW_LIMIT + 1)).hexdigest()


def test_closed_registry_rejects_new_watches(tmp_path):
    registry = FileWatchRegistry(lambda _e: None, auto_start=False)
    registry.close()
    with pytest.raises(EngineProtocolError, match="closed"):
        registry.add("session", tmp_path / "file", None)


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical comparison")
def test_windows_case_alias_preserves_exact_provenance(desktop_runtime):
    services, server, primary, _other, _workspace, tmp = desktop_runtime
    target = tmp / "MiArchivo.txt"
    target.write_text("case insensitive", encoding="utf-8")
    _changeset(services, primary.id, "case", target)
    result = _request(
        server,
        "workspace.file.read",
        {
            "session_id": primary.id,
            "path": str(target).upper(),
            "turn_id": "case",
        },
    )
    assert result["ok"]


def test_archiving_a_session_releases_its_watches(desktop_runtime) -> None:
    # Archivar oculta la sesión igual que cerrarla; sus watches no deben seguir
    # leyendo el disco hasta que se apague el Engine.
    _services, server, primary, _other, workspace, _tmp = desktop_runtime
    target = workspace / "archive.txt"
    target.write_text("one", encoding="utf-8")
    watched = _request(
        server, "workspace.file.watch", {"session_id": primary.id, "path": str(target)}
    )
    assert watched["ok"] is True
    archived = _request(server, "session.archive", {"ref": primary.id})
    assert archived["ok"] is True
    assert not server._desktop._watches._items
    target.write_text("two", encoding="utf-8")
    server._desktop._watches.scan_once()
    assert not [row for row in server.drain_events() if row["event"] == "workspace.file.changed"]


def test_unwatch_is_scoped_to_the_owning_session(desktop_runtime) -> None:
    _services, server, primary, other, workspace, _tmp = desktop_runtime
    target = workspace / "scoped.txt"
    target.write_text("one", encoding="utf-8")
    watch_id = _request(
        server, "workspace.file.watch", {"session_id": primary.id, "path": str(target)}
    )["result"]["watch_id"]

    # Otra sesión no puede retirar el watch, y el watch sigue vivo.
    foreign = _request(
        server, "workspace.file.unwatch", {"session_id": other.id, "watch_id": watch_id}
    )
    assert foreign["error"]["code"] == "PERMISSION_DENIED"
    assert watch_id in server._desktop._watches._items

    # Sin sesión no se atiende.
    missing = _request(server, "workspace.file.unwatch", {"watch_id": watch_id})
    assert missing["error"]["code"] == "INVALID_PARAMS"

    # La dueña sí, y repetirlo (p. ej. tras reiniciar el Engine) no es un error.
    params = {"session_id": primary.id, "watch_id": watch_id}
    assert _request(server, "workspace.file.unwatch", params)["result"] == {}
    assert _request(server, "workspace.file.unwatch", params)["result"] == {}
    assert watch_id not in server._desktop._watches._items


def _junction(target: Path, link: Path) -> None:
    """Directory junction: a Windows reparse point that needs no privilege."""
    import _winapi

    _winapi.CreateJunction(str(target), str(link))
    assert os.lstat(link).st_file_attributes & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows reparse point")
def test_parent_replaced_by_junction_does_not_transfer_authority(desktop_runtime) -> None:
    # La ruta autorizada no cambia de texto, pero su carpeta pasa a ser un
    # reparse point hacia otro sitio: el archivo al que apunta ya no es el que
    # escribió el turno.
    services, server, primary, _other, _workspace, tmp_path = desktop_runtime
    sub = tmp_path / "outside" / "sub"
    elsewhere = tmp_path / "elsewhere"
    sub.mkdir(parents=True)
    elsewhere.mkdir()
    allowed = sub / "allowed.txt"
    allowed.write_text("allowed", encoding="utf-8")
    (elsewhere / "allowed.txt").write_text("secret", encoding="utf-8")
    _changeset(services, primary.id, "turn_junction", allowed)
    allowed.unlink()
    sub.rmdir()
    _junction(elsewhere, sub)
    try:
        denied = _request(
            server,
            "workspace.file.read",
            {"session_id": primary.id, "path": str(allowed), "turn_id": "turn_junction"},
        )
        assert denied["error"]["code"] == "PERMISSION_DENIED"
        assert "secret" not in json.dumps(denied)
    finally:
        os.rmdir(sub)  # quita sólo el enlace, nunca el destino


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows reparse point")
def test_junction_inside_workspace_does_not_reach_outside(desktop_runtime) -> None:
    _services, server, primary, _other, workspace, tmp_path = desktop_runtime
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    _junction(outside, link)
    try:
        denied = _request(
            server,
            "workspace.file.read",
            {"session_id": primary.id, "path": str(link / "secret.txt")},
        )
        assert denied["error"]["code"] == "PERMISSION_DENIED"
        assert "secret" not in json.dumps(denied)
    finally:
        os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows reparse point")
def test_file_watch_detects_parent_replaced_by_junction(tmp_path) -> None:
    events = []
    sub = tmp_path / "sub"
    other = tmp_path / "other"
    sub.mkdir()
    other.mkdir()
    watched = sub / "watched.txt"
    watched.write_text("one", encoding="utf-8")
    (other / "watched.txt").write_text("one", encoding="utf-8")
    watches = FileWatchRegistry(events.append, auto_start=False)
    watches.add("session", watched, "turn")
    watched.unlink()
    sub.rmdir()
    _junction(other, sub)
    try:
        watches.scan_once()
        # Mismo contenido al otro lado: sólo cambia el destino, y eso basta.
        assert events and events[0]["payload"]["state"] == "changed"
    finally:
        watches.close()
        os.rmdir(sub)
