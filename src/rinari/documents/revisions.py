"""Revisiones inmutables de documentos sobre el Artifact Store existente.

Un documento es una cadena de revisiones: el original importado (operación
`import`) y cada resultado de crear o editar, con su padre. Los bytes viven en
el Artifact Store (namespace `documents`); aquí solo hay metadatos. Nada
reescribe una revisión: editar produce otra.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.documents.contracts import (
    MIME,
    DocumentError,
    DocumentErrorCode,
    kind_of_name,
)
from rinari.shared.clock import now_iso

NAMESPACE = "documents"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class Revision:
    id: str
    session_id: str
    document_id: str
    parent_id: str | None
    kind: str
    name: str
    artifact_uri: str
    sha256: str
    byte_count: int
    operation: str
    backend: str | None
    spec_uri: str | None
    state: str
    report: dict[str, Any] | None
    provenance: dict[str, Any] | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "document_id": self.document_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "uri": self.artifact_uri,
            "sha256": self.sha256,
            "size": self.byte_count,
            "operation": self.operation,
            "backend": self.backend,
            "spec_uri": self.spec_uri,
            "state": self.state,
            "report": self.report,
            "provenance": self.provenance,
            "created_at": self.created_at,
        }


def safe_name(name: str) -> str:
    stem, dot, suffix = name.rpartition(".")
    base = _UNSAFE.sub("-", stem if dot else name).strip("-.") or "documento"
    return f"{base[:80]}.{suffix.lower()}" if dot else base[:80]


def _row(row: Any) -> Revision:
    return Revision(
        id=row["id"],
        session_id=row["session_id"],
        document_id=row["document_id"],
        parent_id=row["parent_id"],
        kind=row["kind"],
        name=row["name"],
        artifact_uri=row["artifact_uri"],
        sha256=row["sha256"],
        byte_count=row["byte_count"],
        operation=row["operation"],
        backend=row["backend"],
        spec_uri=row["spec_uri"],
        state=row["state"],
        report=json.loads(row["report_json"]) if row["report_json"] else None,
        provenance=json.loads(row["provenance_json"]) if row["provenance_json"] else None,
        created_at=row["created_at"],
    )


class RevisionStore:
    def __init__(self, artifacts) -> None:
        self._artifacts = artifacts
        self._ctx = artifacts._ctx
        self._db = artifacts._ctx.db

    # -- lectura ---------------------------------------------------------------
    def get(self, revision_id: str, *, session_id: str | None = None) -> Revision:
        row = self._db.query_one("SELECT * FROM document_revisions WHERE id = ?", (revision_id,))
        if row is None or (session_id is not None and row["session_id"] != session_id):
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown revision {revision_id}")
        return _row(row)

    def by_uri(self, uri: str, *, session_id: str) -> Revision | None:
        row = self._db.query_one(
            "SELECT * FROM document_revisions WHERE artifact_uri = ? AND session_id = ? "
            "ORDER BY created_at LIMIT 1",
            (uri, session_id),
        )
        return _row(row) if row else None

    def list(
        self, *, session_id: str, document_id: str | None = None, limit: int = 200
    ) -> list[Revision]:
        if document_id:
            rows = self._db.query(
                "SELECT * FROM document_revisions WHERE session_id = ? AND document_id = ? "
                "ORDER BY created_at, id LIMIT ?",
                (session_id, document_id, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM document_revisions WHERE session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (session_id, limit),
            )
        return [_row(row) for row in rows]

    def path(self, revision: Revision) -> Path:
        record = self._artifacts.meta(revision.artifact_uri)
        path = self._artifacts._storage_path(record.storage_path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != revision.sha256:
            raise DocumentError(
                DocumentErrorCode.REVISION_CONFLICT,
                "Stored revision bytes no longer match their hash",
            )
        return path

    # -- escritura -------------------------------------------------------------
    def import_artifact(self, uri: str, *, session_id: str, name: str | None = None) -> Revision:
        """El original de la sesión como revisión 0 (una sola vez por URI)."""
        existing = self.by_uri(uri, session_id=session_id)
        if existing is not None:
            return existing
        record = self._artifacts.meta(uri)
        if record.session_ref != session_id:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, "The artifact is from another session")
        display = name or _display_name(record.name)
        path = self._artifacts._storage_path(record.storage_path)
        from rinari.documents.security import sniff

        kind = sniff(path)
        return self._insert(
            session_id=session_id,
            document_id=self._ctx.ids.new("doc"),
            parent_id=None,
            kind=kind,
            name=display,
            uri=uri,
            sha256=record.sha256,
            size=record.byte_count,
            operation="import",
            backend=None,
            spec_uri=None,
            provenance={"source": "artifact", "uri": uri},
        )

    def create(
        self,
        data: bytes,
        *,
        session_id: str,
        name: str,
        operation: str,
        backend: str,
        parent: Revision | None = None,
        spec_uri: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Revision:
        kind = kind_of_name(name)
        if kind is None:
            raise DocumentError(DocumentErrorCode.UNSUPPORTED_FORMAT, f"Unsupported output {name}")
        revision_id = self._ctx.ids.new("rev")
        record = self._artifacts.create(
            session_id,
            NAMESPACE,
            f"{revision_id}-{safe_name(name)}",
            data,
            content_type=MIME[kind],
            summary=name,
            provenance=f"documents:{operation}:{backend}",
        )
        return self._insert(
            session_id=session_id,
            document_id=parent.document_id if parent else self._ctx.ids.new("doc"),
            parent_id=parent.id if parent else None,
            kind=kind,
            name=name,
            uri=record.uri(),
            sha256=record.sha256,
            size=record.byte_count,
            operation=operation,
            backend=backend,
            spec_uri=spec_uri,
            provenance=provenance,
            revision_id=revision_id,
        )

    def set_report(self, revision_id: str, report: dict[str, Any]) -> None:
        self._db.execute(
            "UPDATE document_revisions SET report_json = ? WHERE id = ?",
            (json.dumps(report, ensure_ascii=False), revision_id),
        )

    def set_state(self, revision_id: str, state: str) -> None:
        self._db.execute(
            "UPDATE document_revisions SET state = ? WHERE id = ?", (state, revision_id)
        )

    def _insert(self, *, revision_id: str | None = None, **values: Any) -> Revision:
        revision_id = revision_id or self._ctx.ids.new("rev")
        self._db.execute(
            "INSERT INTO document_revisions (id, session_id, document_id, parent_id, kind, name, "
            "artifact_uri, sha256, byte_count, operation, backend, spec_uri, state, "
            "provenance_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (
                revision_id,
                values["session_id"],
                values["document_id"],
                values["parent_id"],
                values["kind"],
                values["name"],
                values["uri"],
                values["sha256"],
                values["size"],
                values["operation"],
                values["backend"],
                values["spec_uri"],
                json.dumps(values["provenance"], ensure_ascii=False)
                if values["provenance"]
                else None,
                now_iso(self._ctx.clock),
            ),
        )
        return self.get(revision_id)


def _display_name(stored: str) -> str:
    head, sep, rest = stored.partition("-")
    if sep and rest and len(head) == 64 and all(c in "0123456789abcdef" for c in head):
        return rest
    return stored
