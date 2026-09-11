"""Stream an authorized existing file into the session artifact store."""

import hashlib
import mimetypes
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

from rinari.artifacts.limits import limit
from rinari.artifacts.store import ArtifactRecord
from rinari.shared.clock import now_iso

_lock = threading.RLock()


def import_file(
    store,
    session_id,
    source,
    *,
    quota=None,
    cancellation=None,
    expected_hash=None,
    maximum=None,
    provenance="authorized-file-import",
):
    quota = quota if quota is not None else limit("quota_bytes", 10 * 1024**3)
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Import requires a regular file, not a link")
    folder = store._root() / session_id / "media"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", session_id):
        raise ValueError("Invalid session identity")
    with _lock:
        if (store._root() / session_id).is_symlink() or folder.is_symlink():
            raise ValueError("Artifact directories cannot be links")
        if not folder.resolve().is_relative_to(store._root().resolve()):
            raise ValueError("Artifact directory escaped storage")
        folder.mkdir(parents=True, exist_ok=True)
        used = sum(p.stat().st_size for p in store._root().glob("*/media/*") if p.is_file())
        if maximum is not None and source.stat().st_size > maximum:
            raise ValueError("File exceeds import limit")
        if used + source.stat().st_size > quota:
            raise ValueError("Media quota exhausted; remove files or increase quota")
        fd, name = tempfile.mkstemp(dir=folder, prefix=".import-")
        temporary = Path(name)
        digest = hashlib.sha256()
        count = 0
        header = b""
        try:
            with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Import requires a regular file")
                while chunk := incoming.read(1024 * 1024):
                    if not header:
                        header = chunk[:64]
                    if cancellation:
                        cancellation.throw_if_cancelled()
                    count += len(chunk)
                    if maximum is not None and count > maximum:
                        raise ValueError("File exceeds import limit")
                    if used + count > quota:
                        raise ValueError("Media quota exhausted")
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
                after = os.fstat(incoming.fileno())
                current = source.stat()

                def identity(s):
                    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

                if identity(before) != identity(after) or identity(after) != identity(current):
                    raise ValueError("File changed during import")
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[-100:] or "file"
            name = digest.hexdigest() + "-" + safe
            destination = folder / name
            if expected_hash is not None and digest.hexdigest() != expected_hash:
                raise ValueError("File changed after validation")
            os.replace(temporary, destination)
            record = ArtifactRecord(
                id=name,
                session_ref=session_id,
                project_root="",
                namespace="media",
                name=name,
                content_type=media_type(header, source.name),
                sha256=digest.hexdigest(),
                byte_count=count,
                storage_path=f"{session_id}/media/{name}",
                summary=source.name,
                provenance=provenance,
                retention="permanent",
                created_at=now_iso(store._ctx.clock),
            )
            store._ctx.db.execute(
                "INSERT OR IGNORE INTO artifacts "
                "(id,session_ref,project_root,namespace,name,content_type,sha256,byte_count,"
                "storage_path,summary,provenance,retention,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.session_ref,
                    record.project_root,
                    record.namespace,
                    record.name,
                    record.content_type,
                    record.sha256,
                    record.byte_count,
                    record.storage_path,
                    record.summary,
                    record.provenance,
                    record.retention,
                    record.created_at,
                ),
            )
            return record
        finally:
            temporary.unlink(missing_ok=True)


def media_type(header, name):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[4:8] == b"ftyp":
        return "video/mp4" if Path(name).suffix.lower() == ".mp4" else "application/octet-stream"
    guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return "application/octet-stream" if guessed.startswith(("image/", "video/")) else guessed
