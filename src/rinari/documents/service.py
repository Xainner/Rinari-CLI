"""DocumentService: coordina inspección, lectura, trabajos y revisiones.

No tiene lógica de formato (eso es de los adaptadores) ni de interfaz. Lo usan
igual las herramientas del modelo y el protocolo del escritorio, siempre
acotado a una sesión: una referencia de otra sesión no existe para esta.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from rinari.documents import capabilities as caps
from rinari.documents import operations
from rinari.documents.contracts import DocumentError, DocumentErrorCode
from rinari.documents.jobs import JobHandle, JobManager
from rinari.documents.revisions import Revision, RevisionStore
from rinari.documents.security import inspect_container

PREVIEW_NAMESPACE = "previews"
MAX_RENDER_PAGES = 60
# El backend va en el nombre del preview: un cambio de renderer no reutiliza
# capturas de otro. Sin guiones, que separan las partes del nombre.
_SLUG = {"office-com": "office", "libreoffice": "libreoffice", "native": "pdfium"}


class DocumentService:
    def __init__(self, artifacts, session_id: str) -> None:
        if not session_id:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, "Documents need a session")
        self.artifacts = artifacts
        self.session_id = session_id
        self.revisions = RevisionStore(artifacts)
        self.jobs = JobManager.for_context(artifacts._ctx)

    # -- referencias -------------------------------------------------------------
    def resolve(self, ref: str, *, name: str | None = None) -> Revision:
        """Una revisión (`rev_…`) o un artefacto de la sesión (`artifact://…`)."""
        if not isinstance(ref, str) or not ref:
            raise DocumentError(DocumentErrorCode.INVALID_SPEC, "A document reference is required")
        if ref.startswith("artifact://"):
            from rinari.artifacts.store import parse_uri

            session, _, _ = parse_uri(ref)
            if session != self.session_id:
                raise DocumentError(
                    DocumentErrorCode.NOT_FOUND, "The artifact is from another session"
                )
            try:
                return self.revisions.import_artifact(ref, session_id=self.session_id, name=name)
            except DocumentError:
                raise
            except Exception as exc:
                raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown artifact {ref}") from exc
        return self.revisions.get(ref, session_id=self.session_id)

    def import_path(self, path: Path) -> Revision:
        """Un archivo autorizado del disco: se copia al Artifact Store antes de tocarlo."""
        from rinari.artifacts.transfer import import_file

        record = import_file(
            self.artifacts, self.session_id, path, provenance=f"documents.import:{path.name}"
        )
        return self.revisions.import_artifact(
            record.uri(), session_id=self.session_id, name=path.name
        )

    # -- lectura -------------------------------------------------------------------
    def inspect(self, ref: str) -> dict[str, Any]:
        revision = self.resolve(ref)
        path = self.revisions.path(revision)
        container = inspect_container(path)
        kind = container.kind
        if kind not in ("pptx", "xlsx", "docx", "pdf"):
            # pptm/xlsm/docm y plantillas: se reconocen, no se editan como su base.
            return {
                "revision": revision.to_dict(),
                "container": container.to_dict(),
                "inspection": None,
                "editable": False,
                "reason": "UNSUPPORTED_FORMAT",
            }
        caps.require(kind, "inspect")
        inspection = operations.inspect(kind, path)
        return {
            "revision": revision.to_dict(),
            "container": container.to_dict(),
            "inspection": inspection,
        }

    def read(
        self, ref: str, selection: str | None = None, cursor: int | None = None
    ) -> dict[str, Any]:
        revision = self.resolve(ref)
        path = self.revisions.path(revision)
        container = inspect_container(path)
        caps.require(container.kind, "read")
        return {
            "revision_id": revision.id,
            **operations.read(container.kind, path, selection, cursor),
        }

    def capabilities(
        self, kind: str | None = None, operation: str | None = None
    ) -> list[dict[str, Any]]:
        return caps.capabilities(kind, operation)

    def list_revisions(self, document_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r.to_dict()
            for r in self.revisions.list(session_id=self.session_id, document_id=document_id)
        ]

    # -- render ------------------------------------------------------------------
    def previews(self, revision: Revision) -> dict[str, Any] | None:
        """Lo ya renderizado para esta revisión exacta (cualquier backend)."""
        rows = self.artifacts._ctx.db.query(
            "SELECT name FROM artifacts WHERE session_ref = ? AND namespace = ? AND name LIKE ? "
            "ORDER BY name",
            (self.session_id, PREVIEW_NAMESPACE, f"{revision.id}-%"),
        )
        if not rows:
            return None
        pages = []
        pdf_uri = None
        backend = None
        for row in rows:
            name = row["name"]
            uri = f"artifact://{self.session_id}/{PREVIEW_NAMESPACE}/{name}"
            stem = name[len(revision.id) + 1 :]
            backend_part, _, rest = stem.partition("-")
            backend = backend or backend_part
            if rest == "render.pdf":
                pdf_uri = uri
            elif rest.startswith("p") and rest.endswith(".png"):
                pages.append({"page": int(rest[1:-4]), "uri": uri})
        pages.sort(key=lambda row: row["page"])
        return {"revision_id": revision.id, "backend": backend, "pdf_uri": pdf_uri, "pages": pages}

    def render(self, ref: str, pages: list[int] | None = None) -> dict[str, Any]:
        """Inicia (o reutiliza) el render de una revisión; devuelve el trabajo."""
        revision = self.resolve(ref)
        kind = revision.kind
        caps.require(kind, "render")
        request = {"revision_id": revision.id, "pages": pages}

        def runner(handle: JobHandle) -> dict[str, Any]:
            existing = self.previews(revision)
            wanted = set(pages or [])
            if (
                existing
                and existing["pages"]
                and (not wanted or wanted <= {p["page"] for p in existing["pages"]})
            ):
                return {**existing, "cached": True}
            handle.phase("render")
            with tempfile.TemporaryDirectory(prefix="rinari-doc-src-") as tmp:
                source = Path(tmp) / f"source.{kind}"
                shutil.copyfile(self.revisions.path(revision), source)
                result = handle.run_worker(
                    "render",
                    {
                        "path": str(source),
                        "kind": kind,
                        "pages": pages,
                        "max_pages": MAX_RENDER_PAGES,
                    },
                )
            handle.phase("publish")
            files = result.pop("_files")
            backend = result["backend"]
            slug = _SLUG.get(backend, "other")
            pdf = self.artifacts.create(
                self.session_id,
                PREVIEW_NAMESPACE,
                f"{revision.id}-{slug}-render.pdf",
                files["pdf"],
                content_type="application/pdf",
                summary=f"Render of {revision.name}",
                provenance=f"documents.render:{revision.id}:{revision.sha256}:{backend}",
            )
            previews = []
            for page in result["pages"]:
                record = self.artifacts.create(
                    self.session_id,
                    PREVIEW_NAMESPACE,
                    f"{revision.id}-{slug}-p{page['page']:04d}.png",
                    files[f"page-{page['page']}"],
                    content_type="image/png",
                    summary=f"{revision.name} p. {page['page']}",
                    provenance=f"documents.render:{revision.id}:{revision.sha256}:{backend}",
                )
                previews.append({**page, "uri": record.uri()})
            if result["page_count"] > len(previews) and not pages:
                status = "partial"
            else:
                status = "succeeded"
            return {
                "_status": status,
                "revision_id": revision.id,
                "sha256": revision.sha256,
                "backend": backend,
                "page_count": result["page_count"],
                "pdf_uri": pdf.uri(),
                "pages": previews,
            }

        if pages is None:
            # Un documento enorme se renderiza en tramos; el resto se pide.
            request["pages"] = None
        return self.jobs.start(
            session_id=self.session_id, operation="render", request=request, runner=runner
        )

    # -- trabajos ------------------------------------------------------------------
    def job(self, job_id: str) -> dict[str, Any]:
        return self.jobs.get(job_id, session_id=self.session_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.jobs.cancel(job_id, session_id=self.session_id)

    def wait(self, job: dict[str, Any], timeout_s: float, cancellation=None) -> dict[str, Any]:
        return self.jobs.wait(
            job["job_id"],
            session_id=self.session_id,
            timeout_s=timeout_s,
            cancellation=cancellation,
        )
