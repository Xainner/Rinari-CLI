"""A .zip (or .tar.gz) attaches as a kept file: the model reads its member
list and unpacks it with artifact.export extract=true (Ideas-2, 24-oct)."""

from __future__ import annotations

import dataclasses
import io
import stat
import tarfile
import zipfile

import pytest

from rinari.artifacts.archives import ArchiveError, extract
from rinari.artifacts.attachments import attachment_prompt, prepare_attachments
from rinari.artifacts.store import ArtifactStore
from rinari.tools.definition import ToolErrorCode
from tests.unit.test_tool_runtime import _ctx, _runtime


def _zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_a_zip_attaches_with_its_member_list_not_its_bytes(app_ctx, tmp_path):
    package = _zip(
        tmp_path / "rinari-revision-ses_x.zip",
        {"events.jsonl": "{}\n", "logs/engine.log": "INFO ok\n"},
    )
    prepared = prepare_attachments(ArtifactStore(app_ctx), "ses_z", [str(package)])
    (item,) = prepared
    assert item.kind == "archive" and item.content_type == "application/zip"
    prompt = attachment_prompt(prepared)
    assert "extract=true" in prompt
    assert "2 files" in prompt and "logs/engine.log\t8" in prompt
    assert "INFO ok" not in prompt  # members are listed, never read


def test_a_tar_gz_attaches_too_and_a_broken_zip_is_refused(app_ctx, tmp_path):
    bundle = tmp_path / "fotos.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        data = b"\x89PNG fake"
        info = tarfile.TarInfo("fotos/a.png")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    (item,) = prepare_attachments(ArtifactStore(app_ctx), "ses_z", [str(bundle)])
    assert item.kind == "archive" and "fotos/a.png" in item.extracted_text
    broken = tmp_path / "roto.zip"
    broken.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="damaged"):
        prepare_attachments(ArtifactStore(app_ctx), "ses_z", [str(broken)])


def _export_setup(app_ctx, tmp_path, archive_path):
    store = ArtifactStore(app_ctx)
    root = tmp_path / "proj"
    root.mkdir()
    ctx = _ctx(tmp_path, root)
    item = prepare_attachments(store, ctx.session_id, [str(archive_path)])[0]
    ctx = dataclasses.replace(ctx, artifact_root=store._root(), artifact_store=store)
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    return item, root, ctx, runtime


def test_export_extract_unpacks_into_a_new_folder_and_never_overwrites(app_ctx, tmp_path):
    package = _zip(tmp_path / "proyecto.zip", {"src/main.py": "print(1)\n", "README.md": "hola"})
    item, root, ctx, runtime = _export_setup(app_ctx, tmp_path, package)

    first = runtime.execute("artifact.export", {"uri": item.source.uri(), "extract": True}, ctx)
    second = runtime.execute("artifact.export", {"uri": item.source.uri(), "extract": True}, ctx)

    assert first.ok, first.error
    assert first.data["path"] == str(root / "proyecto") and first.data["files"] == 2
    assert (root / "proyecto" / "src" / "main.py").read_text(encoding="utf-8") == "print(1)\n"
    assert second.ok and second.data["path"] != first.data["path"]


def test_extract_refuses_escapes_links_and_bombs(tmp_path):
    slip = _zip(tmp_path / "slip.zip", {"../fuera.txt": "x"})
    with pytest.raises(ArchiveError, match="outside"):
        extract(slip, tmp_path / "a")
    assert not (tmp_path / "fuera.txt").exists() and not (tmp_path / "a").exists()

    absolute = _zip(tmp_path / "abs.zip", {"C:/Windows/x.txt": "x"})
    with pytest.raises(ArchiveError, match="outside"):
        extract(absolute, tmp_path / "b")

    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as archive:
        info = zipfile.ZipInfo("enlace")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveError, match="Links"):
        extract(link, tmp_path / "c")
    assert not (tmp_path / "c").exists()

    bomb = _zip(tmp_path / "bomb.zip", {"zeros.bin": b"\0" * (5 * 1024 * 1024)})
    with pytest.raises(ArchiveError, match="ratio"):
        extract(bomb, tmp_path / "d")


def test_extract_is_only_for_archives(app_ctx, tmp_path):
    note = tmp_path / "nota.txt"
    note.write_text("hola", encoding="utf-8")
    item, _root, ctx, runtime = _export_setup(app_ctx, tmp_path, note)
    result = runtime.execute("artifact.export", {"uri": item.source.uri(), "extract": True}, ctx)
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
