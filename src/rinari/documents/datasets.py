"""Datasets de la sesión: metadatos en la base del Engine, datos como artefacto.

Cada dataset es una base DuckDB inmutable (namespace `datasets`) con la tabla
`data`. Un dataset derivado de una consulta guarda su padre y la consulta:
la procedencia de una cifra se puede rehacer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode
from rinari.shared.clock import now_iso

NAMESPACE = "datasets"
_ALIAS = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    session_id: str
    name: str
    source_uri: str | None
    source_sha256: str | None
    parent_id: str | None
    query: str | None
    artifact_uri: str
    row_count: int
    schema: dict[str, Any]
    created_at: str

    @property
    def alias(self) -> str:
        """Nombre SQL estable: el nombre del dataset en minúsculas y sin símbolos."""
        alias = _ALIAS.sub("_", self.name.lower()).strip("_") or "ds"
        if alias[0].isdigit():
            alias = "ds_" + alias
        return alias[:60]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "alias": self.alias,
            "table": "data",
            "source_uri": self.source_uri,
            "parent_id": self.parent_id,
            "query": self.query,
            "uri": self.artifact_uri,
            "row_count": self.row_count,
            "columns": self.schema.get("columns", []),
            "sample": self.schema.get("sample", [])[:20],
            "created_at": self.created_at,
        }


def _row(row: Any) -> Dataset:
    return Dataset(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        source_uri=row["source_uri"],
        source_sha256=row["source_sha256"],
        parent_id=row["parent_id"],
        query=row["query"],
        artifact_uri=row["artifact_uri"],
        row_count=row["row_count"],
        schema=json.loads(row["schema_json"]),
        created_at=row["created_at"],
    )


class DatasetStore:
    def __init__(self, artifacts) -> None:
        self._artifacts = artifacts
        self._ctx = artifacts._ctx
        self._db = artifacts._ctx.db

    def get(self, ref: str, *, session_id: str) -> Dataset:
        row = self._db.query_one(
            "SELECT * FROM document_datasets WHERE session_id = ? AND (id = ? OR name = ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, ref, ref),
        )
        if row is None:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown dataset {ref!r}")
        return _row(row)

    def by_source(self, sha256: str, *, session_id: str) -> Dataset | None:
        row = self._db.query_one(
            "SELECT * FROM document_datasets WHERE session_id = ? AND source_sha256 = ? "
            "AND parent_id IS NULL ORDER BY created_at DESC LIMIT 1",
            (session_id, sha256),
        )
        return _row(row) if row else None

    def list(self, *, session_id: str) -> list[Dataset]:
        rows = self._db.query(
            "SELECT * FROM document_datasets WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        return [_row(row) for row in rows]

    def path(self, dataset: Dataset) -> Path:
        record = self._artifacts.meta(dataset.artifact_uri)
        return self._artifacts._storage_path(record.storage_path)

    def create(
        self,
        source_file: Path,
        *,
        session_id: str,
        name: str,
        profile: dict[str, Any],
        source_uri: str | None = None,
        source_sha256: str | None = None,
        parent_id: str | None = None,
        query: str | None = None,
    ) -> Dataset:
        dataset_id = self._ctx.ids.new("ds")
        record = self._artifacts.create_from_file(
            session_id,
            NAMESPACE,
            f"{dataset_id}.duckdb",
            source_file,
            content_type="application/vnd.duckdb",
            summary=f"Dataset {name}",
            provenance=f"documents.dataset:{source_uri or parent_id or ''}",
        )
        schema = {"columns": profile.get("columns", []), "sample": profile.get("sample", [])[:20]}
        self._db.execute(
            "INSERT INTO document_datasets (id, session_id, name, source_uri, source_sha256, "
            "parent_id, query, artifact_uri, row_count, schema_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                session_id,
                name,
                source_uri,
                source_sha256,
                parent_id,
                query,
                record.uri(),
                int(profile.get("row_count") or 0),
                json.dumps(schema, ensure_ascii=False, default=str),
                now_iso(self._ctx.clock),
            ),
        )
        return self.get(dataset_id, session_id=session_id)


__all__ = ["Dataset", "DatasetStore"]
