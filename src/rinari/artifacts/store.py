"""Artifact Store: file-backed spill with stable URIs and metadata (phase 4).

Large or durable outputs (test logs, browser captures, big tool outputs) are
written to the artifacts directory with a stable URI:

    artifact://<session>/<namespace>/<name>

SQLite holds the metadata (hash, content type, size, provenance, retention);
the bytes live on disk under ``<home>/artifacts``. This is what lets context
compaction and retrieval reference big outputs by URI instead of inlining
them.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.shared.errors import NotFoundError, ValidationFailureError

ARTIFACT_SCHEME = "artifact://"

RETENTION_SESSION = "session"
RETENTION_PROJECT = "project"
RETENTION_PERMANENT = "permanent"
RETENTIONS = (RETENTION_SESSION, RETENTION_PROJECT, RETENTION_PERMANENT)

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_INLINE_BYTES = 256 * 1024
SEARCH_CONTENT_BYTES = 512 * 1024


class ArtifactURIError(ValidationFailureError):
    """Malformed artifact URI."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    session_ref: str
    project_root: str
    namespace: str
    name: str
    content_type: str
    sha256: str
    byte_count: int
    storage_path: str
    summary: str = ""
    provenance: str = ""
    retention: str = RETENTION_SESSION
    created_at: str = ""

    def uri(self) -> str:
        return f"{ARTIFACT_SCHEME}{self.session_ref}/{self.namespace}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri(),
            "id": self.id,
            "session": self.session_ref,
            "project_root": self.project_root,
            "namespace": self.namespace,
            "name": self.name,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "summary": self.summary,
            "provenance": self.provenance,
            "retention": self.retention,
            "created_at": self.created_at,
        }


def build_uri(session_id: str, namespace: str, name: str) -> str:
    return f"{ARTIFACT_SCHEME}{session_id.strip()}/{namespace.strip()}/{name.strip()}"


def parse_uri(uri: str) -> tuple[str, str, str]:
    """Parse ``artifact://session/namespace/name`` -> (session, namespace, name)."""
    if not uri or not uri.startswith(ARTIFACT_SCHEME):
        raise ArtifactURIError(f"Not an artifact URI: {uri!r}")
    rest = uri[len(ARTIFACT_SCHEME) :]
    parts = rest.split("/")
    if len(parts) != 3 or not all(parts):
        raise ArtifactURIError(f"Expected artifact://<session>/<namespace>/<name>, got {uri!r}")
    for part in parts:
        if not _SEGMENT.match(part):
            raise ArtifactURIError(f"Invalid path segment in URI: {part!r}")
    return parts[0], parts[1], parts[2]


class ArtifactStore:
    """File-backed artifact content + SQLite metadata.

    All paths are confined to the home artifacts dir; URIs are the only
    public address form.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    # -- paths -----------------------------------------------------------

    def _root(self) -> Path:
        return self._ctx.layout.artifacts_dir

    def _storage_path(self, storage_path: str) -> Path:
        candidate = self._root() / storage_path
        try:
            candidate.relative_to(self._root())
        except ValueError as exc:
            raise ArtifactURIError(f"Artifact outside artifacts dir: {storage_path!r}") from exc
        return candidate

    # -- write -----------------------------------------------------------

    def create(
        self,
        session_id: str,
        namespace: str,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        summary: str = "",
        provenance: str = "",
        project_root: str = "",
        retention: str = RETENTION_SESSION,
    ) -> ArtifactRecord:
        if not _SEGMENT.match(namespace or ""):
            raise ArtifactURIError(f"Invalid artifact namespace: {namespace!r}")
        if not _SEGMENT.match(name or ""):
            raise ArtifactURIError(f"Invalid artifact name: {name!r}")
        if retention not in RETENTIONS:
            raise ArtifactURIError(f"Invalid retention {retention!r}; use one of {RETENTIONS}")
        digest = sha256(data).hexdigest()
        storage_path = f"{session_id.strip()}/{namespace}/{name}"
        target = self._storage_path(storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record = ArtifactRecord(
            id=name,
            session_ref=session_id.strip(),
            project_root=project_root,
            namespace=namespace,
            name=name,
            content_type=content_type,
            sha256=digest,
            byte_count=len(data),
            storage_path=storage_path,
            summary=summary,
            provenance=provenance,
            retention=retention,
            created_at=now_iso(self._ctx.clock),
        )
        self._ctx.db.execute(
            """
            INSERT INTO artifacts (
                id, session_ref, project_root, namespace, name, content_type,
                sha256, byte_count, storage_path, summary, provenance,
                retention, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_ref, namespace, id) DO UPDATE SET
                name = excluded.name,
                content_type = excluded.content_type,
                sha256 = excluded.sha256,
                byte_count = excluded.byte_count,
                storage_path = excluded.storage_path,
                summary = excluded.summary,
                provenance = excluded.provenance,
                retention = excluded.retention,
                created_at = excluded.created_at
            """,
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

    def create_text(
        self,
        session_id: str,
        namespace: str,
        name: str,
        text: str,
        **kwargs: Any,
    ) -> ArtifactRecord:
        kwargs.setdefault("content_type", "text/plain")
        return self.create(session_id, namespace, name, text.encode("utf-8"), **kwargs)

    # -- read ------------------------------------------------------------

    def meta(self, uri: str) -> ArtifactRecord:
        session, namespace, name = parse_uri(uri)
        row = self._ctx.db.query_one(
            "SELECT * FROM artifacts WHERE session_ref = ? AND namespace = ? AND name = ?",
            (session, namespace, name),
        )
        if row is None:
            raise NotFoundError(f"Artifact not found: {uri}")
        return _record_from_row(row)

    def get(self, uri: str) -> bytes:
        record = self.meta(uri)
        return self._storage_path(record.storage_path).read_bytes()

    def read_text(self, uri: str, max_bytes: int = MAX_INLINE_BYTES) -> tuple[str, bool]:
        data = self.get(uri)
        truncated = len(data) > max_bytes
        return data[:max_bytes].decode("utf-8", errors="replace"), truncated

    def lines(
        self, uri: str, start_line: int = 0, end_line: int | None = None
    ) -> tuple[list[str], bool]:
        data = self.get(uri)
        all_lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(all_lines)
        start = max(0, start_line)
        end = total if end_line is None else min(total, end_line)
        selected = all_lines[start:end]
        truncated = len(selected) != total
        return selected, truncated

    # -- discover --------------------------------------------------------

    def list(
        self,
        *,
        session_id: str | None = None,
        project_root: str | None = None,
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        where: list[str] = []
        args: list[Any] = []
        if session_id is not None:
            where.append("session_ref = ?")
            args.append(session_id)
        if project_root is not None:
            where.append("project_root = ?")
            args.append(project_root)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._ctx.db.query(
            f"SELECT * FROM artifacts{clause} ORDER BY created_at DESC LIMIT ?",
            (*args, limit),
        )
        return [_record_from_row(row) for row in rows]

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search by name/summary, then by bounded content scan.

        Metadata hits win; a full content scan (size-capped, bounded number
        of files) fills the remaining slots.
        """
        q = query.strip().lower()
        if not q:
            return []
        like = f"%{q}%"
        meta_rows = self._ctx.db.query(
            "SELECT * FROM artifacts WHERE name LIKE ? OR summary LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (like, like, limit),
        )
        results: list[dict[str, Any]] = []
        for row in meta_rows:
            record = _record_from_row(row)
            results.append({**record.to_dict(), "matched": "metadata"})
            if len(results) >= limit:
                return results

        candidates = self._ctx.db.query(
            "SELECT * FROM artifacts "
            "WHERE byte_count <= ? AND name NOT LIKE ? AND summary NOT LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (SEARCH_CONTENT_BYTES, like, like, limit * 10),
        )
        for row in candidates:
            record = _record_from_row(row)
            path = self._storage_path(record.storage_path)
            if not path.exists():
                continue
            try:
                if q not in path.read_bytes().decode("utf-8", errors="replace").lower():
                    continue
            except OSError:
                continue
            results.append({**record.to_dict(), "matched": "content"})
            if len(results) >= limit:
                break
        return results

    # -- maintenance -----------------------------------------------------

    def export(self, uri: str, dest: Path) -> Path:
        record = self.meta(uri)
        dest = dest.expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._storage_path(record.storage_path), dest)
        return dest

    def remove(self, uri: str) -> bool:
        try:
            record = self.meta(uri)
        except NotFoundError:
            return False
        path = self._storage_path(record.storage_path)
        if path.exists():
            path.unlink()
        self._ctx.db.execute(
            "DELETE FROM artifacts WHERE session_ref = ? AND namespace = ? AND name = ?",
            (record.session_ref, record.namespace, record.name),
        )
        _rmdir_quiet(path.parent)
        return True

    def gc(self, *, session_id: str | None = None) -> int:
        """Drop artifacts of sessions that no longer exist (retention=session).

        Conservative: only session-scoped artifacts whose session row is gone
        are removed; project/permanent retention always survives.
        """
        active = {row["id"] for row in self._ctx.db.query("SELECT id FROM sessions")}
        deleted = 0
        rows = self._ctx.db.query(
            "SELECT * FROM artifacts WHERE retention = ?", (RETENTION_SESSION,)
        )
        for row in rows:
            record = _record_from_row(row)
            if session_id is not None and record.session_ref != session_id:
                continue
            if record.session_ref in active:
                continue
            path = self._storage_path(record.storage_path)
            if path.exists():
                path.unlink()
            self._ctx.db.execute(
                "DELETE FROM artifacts WHERE session_ref = ? AND namespace = ? AND name = ?",
                (record.session_ref, record.namespace, record.name),
            )
            deleted += 1
        return deleted


def _record_from_row(row: Any) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        session_ref=row["session_ref"],
        project_root=row["project_root"] or "",
        namespace=row["namespace"],
        name=row["name"],
        content_type=row["content_type"],
        sha256=row["sha256"],
        byte_count=row["byte_count"],
        storage_path=row["storage_path"],
        summary=row["summary"] or "",
        provenance=row["provenance"] or "",
        retention=row["retention"],
        created_at=row["created_at"],
    )


def _rmdir_quiet(path: Path) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        path.rmdir()


__all__ = [
    "ARTIFACT_SCHEME",
    "ArtifactRecord",
    "ArtifactStore",
    "ArtifactURIError",
    "build_uri",
    "parse_uri",
]
