"""file.present: hand files, folders and pages to the owner (plan 0.4.0 #35)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.present import present_tools

TOOL = present_tools()[0]


@pytest.fixture
def seen() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def ctx(tmp_path: Path, seen) -> ToolContext:
    root = tmp_path / "project"
    root.mkdir()
    return ToolContext(
        session_id="session-1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(root, (root,)),
        limits=ProcessLimits(),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        activity_sink=lambda name, payload: seen.append((name, payload)),
        tool_call_id="c1",
    )


def _files(root: Path) -> None:
    (root / "cancion.mp3").write_bytes(b"ID3" + b"\0" * 64)
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    (root / "informe.pdf").write_bytes(b"%PDF-1.7\n")
    (root / "ventas.xlsx").write_bytes(b"PK\x03\x04" + b"\0" * 32)
    (root / "datos.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "sitio.html").write_text("<h1>hola</h1>", encoding="utf-8")
    (root / "notas.md").write_text("# Notas", encoding="utf-8")
    (root / "main.py").write_text("print(1)", encoding="utf-8")
    with zipfile.ZipFile(root / "entrega.zip", "w") as archive:
        archive.writestr("a.txt", "a")
        archive.writestr("b/c.txt", "c")
    (root / "salida").mkdir()
    (root / "salida" / "uno.txt").write_text("1", encoding="utf-8")
    (root / "salida" / "fotos").mkdir()


def test_every_kind_gets_its_card(ctx, seen):
    _files(ctx.cwd)
    names = [
        "cancion.mp3",
        "logo.png",
        "informe.pdf",
        "ventas.xlsx",
        "datos.csv",
        "sitio.html",
        "notas.md",
        "main.py",
        "entrega.zip",
        "salida",
    ]
    items = [{"path": name} for name in names]
    items += [
        {"url": "http://localhost:5173/"},
        {"url": "https://qwen.ai/blog", "caption": "  la fuente  "},
    ]
    result = TOOL.handler({"items": items}, ctx)
    assert result.ok, result.error
    assert result.data["presented"] == 12
    name, payload = seen[0]
    assert name == "file.presented" and payload["present_id"] == "c1"
    kinds = [item["kind"] for item in payload["items"]]
    assert kinds == [
        "audio",
        "image",
        "pdf",
        "office",
        "sheet",
        "html",
        "markdown",
        "text",
        "archive",
        "folder",
        "preview",
        "web",
    ]
    by_name = {item["name"]: item for item in payload["items"]}
    assert by_name["entrega.zip"]["entries"] == ["a.txt", "b/c.txt"]
    assert by_name["salida"] == {
        "path": str((ctx.cwd / "salida").resolve()),
        "name": "salida",
        "caption": "",
        "kind": "folder",
        "entry_count": 2,
        "entries": ["fotos/", "uno.txt"],
    }
    assert by_name["cancion.mp3"]["mime"] == "audio/mpeg"
    assert by_name["qwen.ai"]["caption"] == "la fuente"


def test_saved_outputs_of_the_conversation_can_be_presented(ctx, seen):
    saved = ctx.artifact_root / ctx.session_id / "media" / "clip.mp4"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 32)
    result = TOOL.handler({"items": [{"path": "artifact://session-1/media/clip.mp4"}]}, ctx)
    assert result.ok, result.error
    item = seen[0][1]["items"][0]
    assert item["kind"] == "video" and item["uri"] == "artifact://session-1/media/clip.mp4"


def test_nothing_outside_the_conversation_is_presented(ctx, seen, tmp_path):
    outside = tmp_path / "secreto.txt"
    outside.write_text("x", encoding="utf-8")
    for bad in (
        [{"path": str(outside)}],
        [{"path": "artifact://otra-sesion/x.png"}],
        [{"path": "no-existe.mp3"}],
        [{"url": "file:///C:/x.txt"}],
        [{"path": "a", "url": "https://x.dev"}],
        [],
    ):
        result = TOOL.handler({"items": bad}, ctx)
        assert not result.ok, bad
    assert seen == []


def _stored_ctx(ctx: ToolContext, app_ctx):
    """The same context with the Engine's artifact store, as in a real turn."""
    from dataclasses import replace

    from rinari.artifacts.store import ArtifactStore

    store = ArtifactStore(app_ctx)
    # The owner's conversation could read the Engine's folder (full access).
    sandbox = FilesystemSandbox(ctx.cwd, (ctx.cwd,), unrestricted_reads=True)
    return replace(ctx, artifact_root=store._root(), artifact_store=store, sandbox=sandbox), store


def test_a_loose_file_in_the_conversation_gets_a_uri_the_store_opens(ctx, seen, app_ctx):
    """A browser capture used to leave a loose copy in the conversation's folder;
    presenting it made `artifact://<session>/<file>`, which nothing could open."""
    ctx, store = _stored_ctx(ctx, app_ctx)
    loose = store._root() / ctx.session_id / "screenshot-1792631230.png"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    result = TOOL.handler({"items": [{"path": str(loose)}]}, ctx)
    assert result.ok, result.error
    uri = seen[0][1]["items"][0]["uri"]
    assert uri.startswith(f"artifact://{ctx.session_id}/media/")
    assert store.get(uri) == loose.read_bytes()


def test_a_stored_file_is_named_by_its_own_uri(ctx, seen, app_ctx):
    from rinari.artifacts.transfer import import_file

    ctx, store = _stored_ctx(ctx, app_ctx)
    source = ctx.cwd / "logo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    record = import_file(store, ctx.session_id, source)
    stored = store._storage_path(record.storage_path)
    result = TOOL.handler({"items": [{"path": str(stored)}]}, ctx)
    assert result.ok, result.error
    assert seen[0][1]["items"][0]["uri"] == record.uri()
    assert len(store.list(session_id=ctx.session_id)) == 1  # nothing imported twice
