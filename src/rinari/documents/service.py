"""DocumentService: coordina inspección, lectura, trabajos y revisiones.

No tiene lógica de formato (eso es de los adaptadores) ni de interfaz. Lo usan
igual las herramientas del modelo y el protocolo del escritorio, siempre
acotado a una sesión: una referencia de otra sesión no existe para esta.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from rinari.documents import capabilities as caps
from rinari.documents import operations
from rinari.documents.contracts import (
    CHECK_FAILED,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PARTIAL,
    CHECK_PASSED,
    PRESERVATION_POLICIES,
    PRESERVE_STRICT,
    SCHEMA_VERSION,
    Check,
    DocumentError,
    DocumentErrorCode,
    ValidationReport,
    kind_of_name,
)
from rinari.documents.jobs import JobHandle, JobManager, run_inline
from rinari.documents.revisions import NAMESPACE, Revision, RevisionStore, safe_name
from rinari.documents.security import inspect_container
from rinari.shared.clock import now_iso

PREVIEW_NAMESPACE = "previews"
MAX_RENDER_PAGES = 60
MAX_RESOURCES = 40
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
FINDING_SEVERITIES = ("critical", "error", "warning", "suggestion")
DELIVERABLE_DRAFT = "draft"
DELIVERABLE_FINAL = "final"
DELIVERABLE_ACCEPTED_DRAFT = "accepted_draft"
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
            return self._render_revision(handle, revision, pages)

        return self.jobs.start(
            session_id=self.session_id, operation="render", request=request, runner=runner
        )

    def _render_revision(
        self, handle: JobHandle, revision: Revision, pages: list[int] | None
    ) -> dict[str, Any]:
        kind = revision.kind
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
        partial = result["page_count"] > len(previews) and not pages
        status = "partial" if partial else "succeeded"
        return {
            "_status": status,
            "revision_id": revision.id,
            "sha256": revision.sha256,
            "backend": backend,
            "page_count": result["page_count"],
            "pdf_uri": pdf.uri(),
            "pages": previews,
        }

    # -- autoría ----------------------------------------------------------------------
    def templates(self, kind: str | None = None) -> dict[str, Any]:
        from rinari.documents.adapters import pptx_build

        catalog: dict[str, Any] = {}
        if kind in (None, "pptx"):
            catalog["pptx"] = {
                **pptx_build.template_catalog(),
                "edit_operations": _edit_catalog(),
            }
        return catalog

    def resources(self, refs: Any, workdir: Path) -> dict[str, str]:
        """Imágenes de la sesión (`artifact://`) copiadas y comprobadas para el worker."""
        if refs in (None, {}):
            return {}
        if not isinstance(refs, dict) or len(refs) > MAX_RESOURCES:
            raise DocumentError(
                DocumentErrorCode.INVALID_SPEC,
                f"resources is a map of up to {MAX_RESOURCES} name -> artifact://",
            )
        from PIL import Image

        out: dict[str, str] = {}
        for index, (key, uri) in enumerate(refs.items()):
            if not isinstance(uri, str) or not uri.startswith(f"artifact://{self.session_id}/"):
                raise DocumentError(
                    DocumentErrorCode.UNSAFE_EXTERNAL_RESOURCE,
                    f"resource {key!r} must be an artifact of this session",
                )
            try:
                record = self.artifacts.meta(uri)
            except Exception as exc:
                raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown {uri}") from exc
            if record.byte_count > MAX_IMAGE_BYTES:
                raise DocumentError(
                    DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, f"resource {key!r} is too large"
                )
            source = self.artifacts._storage_path(record.storage_path)
            try:
                with Image.open(source) as image:
                    image.verify()
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError("too many pixels")
                    suffix = (image.format or "png").lower()
            except Exception as exc:
                raise DocumentError(
                    DocumentErrorCode.UNSUPPORTED_FORMAT,
                    f"resource {key!r} is not a usable image",
                ) from exc
            target = workdir / f"resource-{index}.{suffix}"
            shutil.copyfile(source, target)
            out[str(key)] = str(target)
        return out

    def _store_spec(self, spec: dict[str, Any]) -> str:
        text = json.dumps(spec, ensure_ascii=False, indent=1)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        record = self.artifacts.create_text(
            self.session_id,
            NAMESPACE,
            f"spec-{digest}.json",
            text,
            content_type="application/json",
            summary="DeckSpec",
            provenance="documents.create",
        )
        return record.uri()

    def create(
        self,
        spec: Any,
        *,
        output_name: str | None = None,
        resources: Any = None,
        render: bool = True,
        expected_text: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise DocumentError(DocumentErrorCode.INVALID_SPEC, "spec must be an object")
        kind = str(spec.get("kind") or "pptx")
        spec = {k: v for k, v in spec.items() if k != "kind"}
        name = safe_name(output_name or f"{spec.get('title') or 'Presentacion'}.{kind}")
        if kind_of_name(name) != kind:
            name = f"{name.rsplit('.', 1)[0]}.{kind}"
        caps.require(kind, "create")
        operation = f"{kind}.create"
        if operation not in operations.REGISTRY:
            raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot create {kind} yet")
        request = {"kind": kind, "output_name": name, "render": render}

        def runner(handle: JobHandle) -> dict[str, Any]:
            handle.phase("build")
            spec_uri = self._store_spec(spec)
            with tempfile.TemporaryDirectory(prefix="rinari-doc-res-") as tmp:
                files = self.resources(resources, Path(tmp))
                result = handle.run_worker(
                    operation,
                    {"spec": spec, "resources": files, "expected_text": expected_text},
                )
            handle.phase("publish")
            data = result.pop("_files")["document"]
            revision = self.revisions.create(
                data,
                session_id=self.session_id,
                name=name,
                operation="create",
                backend="python-pptx",
                spec_uri=spec_uri,
                provenance={"spec_uri": spec_uri, "resources": sorted((resources or {}).values())},
            )
            return self._after_build(handle, revision, result, render)

        return self.jobs.start(
            session_id=self.session_id, operation="create", request=request, runner=runner
        )

    def edit(
        self,
        ref: str,
        operations_: Any,
        *,
        expected_sha256: str | None = None,
        preservation: str = PRESERVE_STRICT,
        output_name: str | None = None,
        resources: Any = None,
        render: bool = True,
        expected_text: list[str] | None = None,
    ) -> dict[str, Any]:
        revision = self.resolve(ref)
        kind = revision.kind
        if expected_sha256 and expected_sha256 != revision.sha256:
            raise DocumentError(
                DocumentErrorCode.REVISION_CONFLICT,
                "The document is not the expected version",
                details={"sha256": revision.sha256},
                action="Inspect the current revision and retry",
            )
        if preservation not in PRESERVATION_POLICIES:
            raise DocumentError(
                DocumentErrorCode.INVALID_SPEC,
                f"preservation must be one of {', '.join(PRESERVATION_POLICIES)}",
            )
        caps.require(kind, "edit")
        operation = f"{kind}.edit"
        if operation not in operations.REGISTRY:
            raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot edit {kind} yet")
        if kind == "pptx":
            from rinari.documents.adapters import pptx_edit

            pptx_edit.validate(operations_)
        name = safe_name(output_name or revision.name)
        if kind_of_name(name) != kind:
            raise DocumentError(DocumentErrorCode.INVALID_SPEC, f"output_name must end in .{kind}")
        request = {
            "revision_id": revision.id,
            "operations": len(operations_),
            "preservation": preservation,
        }

        def runner(handle: JobHandle) -> dict[str, Any]:
            handle.phase("build")
            with tempfile.TemporaryDirectory(prefix="rinari-doc-edit-") as tmp:
                source = Path(tmp) / f"source.{kind}"
                shutil.copyfile(self.revisions.path(revision), source)
                files = self.resources(resources, Path(tmp))
                result = handle.run_worker(
                    operation,
                    {
                        "path": str(source),
                        "operations": operations_,
                        "resources": files,
                        "preservation": preservation,
                        "expected_text": expected_text,
                    },
                )
            handle.phase("publish")
            data = result.pop("_files")["document"]
            child = self.revisions.create(
                data,
                session_id=self.session_id,
                name=name,
                operation="edit",
                backend="python-pptx",
                parent=revision,
                provenance={
                    "parent": revision.id,
                    "parent_sha256": revision.sha256,
                    "operations": operations_,
                },
            )
            return self._after_build(handle, child, result, render)

        return self.jobs.start(
            session_id=self.session_id, operation="edit", request=request, runner=runner
        )

    def _after_build(
        self, handle: JobHandle, revision: Revision, result: dict[str, Any], render: bool
    ) -> dict[str, Any]:
        """Informe estático de la revisión nueva y, si se pidió y se puede, su render."""
        extra = {
            key: result[key]
            for key in ("changes", "semantic_diff", "plan_findings", "slides")
            if key in result
        }
        checks = dict(result.get("checks") or {})
        if "preservation" in result:
            preservation = result["preservation"]
            checks["preservation"] = {
                "status": preservation["status"],
                "evidence": {
                    "policy": preservation["policy"],
                    "changed": len(preservation["diff"]["changed"]),
                    "added": len(preservation["diff"]["added"]),
                    "removed": len(preservation["diff"]["removed"]),
                    "unchanged": preservation["diff"]["unchanged"],
                },
                "findings": preservation["unexpected"],
            }
        plan = result.get("plan_findings") or []
        if plan:
            layout = checks.get("layout") or {"status": CHECK_PASSED}
            errors = [f for f in plan if f.get("severity") == "error"]
            checks["layout"] = {
                **layout,
                "status": CHECK_FAILED if errors else layout["status"],
                "findings": [*(layout.get("findings") or []), *plan][:200],
            }
        report = self._report(revision, checks, extra)
        out: dict[str, Any] = {"revision": revision.to_dict(), "report": report}
        if render:
            if caps.capability(revision.kind, "render").get("available"):
                handle.phase("render")
                rendered = self._render_revision(handle, revision, None)
                rendered.pop("_status", None)
                out["render"] = rendered
                report = self._report(revision, checks, extra)
                out["report"] = report
            else:
                out["render"] = {"status": "unavailable", "reason": "BACKEND_UNAVAILABLE"}
        return out

    # -- verificación --------------------------------------------------------------
    def _visual(self, revision: Revision, review: dict[str, Any] | None) -> dict[str, Any]:
        previews = self.previews(revision)
        if not previews or not previews["pages"]:
            available = caps.capability(revision.kind, "render").get("available")
            return Check(
                CHECK_NOT_RUN, reason="NOT_RENDERED" if available else "RENDER_UNAVAILABLE"
            ).to_dict()
        rendered = {p["page"] for p in previews["pages"]}
        if not review:
            return Check(
                CHECK_NOT_RUN,
                reason="NOT_REVIEWED",
                evidence={"rendered_pages": len(rendered), "backend": previews["backend"]},
            ).to_dict()
        reviewed = set(review.get("pages") or []) & rendered
        findings = review.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("critical", "error")]
        page_count = review.get("page_count") or len(rendered)
        if blocking:
            status = CHECK_FAILED
        elif len(reviewed) >= page_count:
            status = CHECK_PASSED
        else:
            status = CHECK_PARTIAL
        return Check(
            status,
            evidence={
                "reviewed_pages": sorted(reviewed),
                "page_count": page_count,
                "backend": previews["backend"],
                "reviewed_at": review.get("at"),
                "reviewer": review.get("reviewer"),
            },
            findings=findings[:100],
        ).to_dict()

    def _report(
        self,
        revision: Revision,
        checks: dict[str, dict[str, Any]],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.revisions.get(revision.id).report or {}
        review = previous.get("visual_review")
        merged = {**{k: v for k, v in (previous.get("checks") or {}).items()}, **checks}
        merged["visual"] = self._visual(revision, review)
        if revision.kind == "pptx":
            merged.setdefault("formulas", Check(CHECK_NOT_APPLICABLE).to_dict())
        if revision.parent_id is None:
            merged.setdefault("preservation", Check(CHECK_NOT_APPLICABLE).to_dict())
        report = ValidationReport(
            revision.id,
            revision.sha256,
            {
                name: Check(
                    value.get("status", CHECK_NOT_RUN),
                    value.get("reason"),
                    value.get("evidence") or {},
                    value.get("findings") or [],
                )
                for name, value in merged.items()
            },
        ).to_dict()
        report["deliverable_state"] = revision.state
        for key in ("changes", "semantic_diff", "plan_findings", "slides", "visual_review"):
            if extra and key in extra:
                report[key] = extra[key]
            elif key in previous:
                report[key] = previous[key]
        if review:
            report["visual_review"] = review
        report["schema_version"] = SCHEMA_VERSION
        self.revisions.set_report(revision.id, report)
        return report

    def report(self, ref: str) -> dict[str, Any]:
        revision = self.resolve(ref)
        if revision.report:
            return self._report(revision, {})
        return self.validate(ref)

    def validate(self, ref: str, expected_text: list[str] | None = None) -> dict[str, Any]:
        """Estructura, composición y contenido de una revisión, con evidencia."""
        revision = self.resolve(ref)
        path = self.revisions.path(revision)
        if revision.kind != "pptx":
            container = inspect_container(path)
            checks = {
                "structure": Check(
                    CHECK_PASSED,
                    evidence={"kind": container.kind, "risks": len(container.risks)},
                ).to_dict(),
                "content": Check(CHECK_NOT_RUN, reason="UNSUPPORTED_FEATURE").to_dict(),
            }
            return self._report(revision, checks)
        with tempfile.TemporaryDirectory(prefix="rinari-doc-val-") as tmp:
            source = Path(tmp) / "source.pptx"
            shutil.copyfile(path, source)
            result = run_inline(
                "pptx.validate", {"path": str(source), "expected_text": expected_text}
            )
        result.pop("_files", None)
        return self._report(revision, result["checks"])

    def review(
        self,
        ref: str,
        *,
        pages: Any,
        findings: Any = None,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        """Registra la revisión visual hecha sobre los renders de esta revisión exacta."""
        revision = self.resolve(ref)
        previews = self.previews(revision)
        if not previews or not previews["pages"]:
            raise DocumentError(
                DocumentErrorCode.VALIDATION_FAILED,
                "There are no renders of this revision to review",
                action="Render it first (documents.render) and look at the pages",
            )
        rendered = {p["page"] for p in previews["pages"]}
        if not isinstance(pages, list) or not pages or not all(isinstance(p, int) for p in pages):
            raise DocumentError(DocumentErrorCode.INVALID_SPEC, "pages is the list you reviewed")
        missing = sorted(set(pages) - rendered)
        if missing:
            raise DocumentError(
                DocumentErrorCode.VALIDATION_FAILED,
                f"Pages {missing} were not rendered for this revision",
            )
        clean: list[dict[str, Any]] = []
        for item in findings or []:
            if not isinstance(item, dict) or item.get("severity") not in FINDING_SEVERITIES:
                raise DocumentError(
                    DocumentErrorCode.INVALID_SPEC,
                    f"each finding needs severity ({', '.join(FINDING_SEVERITIES)}) and message",
                )
            if item.get("page") is not None and item["page"] not in rendered:
                raise DocumentError(
                    DocumentErrorCode.INVALID_SPEC, f"finding page {item['page']} not rendered"
                )
            clean.append(
                {
                    "severity": item["severity"],
                    "message": str(item.get("message") or "")[:600],
                    "page": item.get("page"),
                    "shape_id": item.get("shape_id"),
                    "category": str(item.get("category") or "design")[:40],
                    "fix": str(item.get("fix") or "")[:400] or None,
                }
            )
        previous = self.revisions.get(revision.id).report or {}
        prior = previous.get("visual_review") or {}
        review = {
            "pages": sorted(set(prior.get("pages") or []) | set(pages)),
            "page_count": _page_count(previews, prior),
            "findings": [
                *[f for f in prior.get("findings") or [] if f.get("page") not in set(pages)],
                *clean,
            ],
            "reviewer": (reviewer or "model")[:60],
            "backend": previews["backend"],
            "sha256": revision.sha256,
            "at": now_iso(self.artifacts._ctx.clock),
        }
        report = {**previous, "visual_review": review}
        self.revisions.set_report(revision.id, report)
        return self._report(self.revisions.get(revision.id), {})

    def diff(self, before: str, after: str) -> dict[str, Any]:
        a, b = self.resolve(before), self.resolve(after)
        if a.kind != b.kind or a.kind not in ("pptx", "docx", "xlsx"):
            raise DocumentError(
                DocumentErrorCode.UNSUPPORTED_FEATURE,
                "Diff compares two OOXML revisions of one kind",
            )
        with tempfile.TemporaryDirectory(prefix="rinari-doc-diff-") as tmp:
            first = Path(tmp) / f"a.{a.kind}"
            second = Path(tmp) / f"b.{b.kind}"
            shutil.copyfile(self.revisions.path(a), first)
            shutil.copyfile(self.revisions.path(b), second)
            result = run_inline(
                "diff", {"before": str(first), "after": str(second), "kind": a.kind}
            )
        result.pop("_files", None)
        return {"before": a.id, "after": b.id, **result}

    def finalize(self, ref: str, *, accept_partial: bool = False) -> dict[str, Any]:
        """Entrega la revisión si sus checks lo permiten; si no, dice qué falta."""
        revision = self.resolve(ref)
        report = self.report(revision.id)
        blocked: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for name, check in report["checks"].items():
            status = check["status"]
            if status == CHECK_FAILED:
                blocked.append({"check": name, "status": status, "reason": check.get("reason")})
            elif status in (CHECK_PARTIAL, CHECK_NOT_RUN) and name in (
                "structure",
                "layout",
                "visual",
                "preservation",
            ):
                partial.append({"check": name, "status": status, "reason": check.get("reason")})
        if blocked or (partial and not accept_partial):
            return {
                "finalized": False,
                "revision": revision.to_dict(),
                "blocked_by": blocked,
                "pending": partial,
                "report": report,
            }
        state = DELIVERABLE_ACCEPTED_DRAFT if partial else DELIVERABLE_FINAL
        self.revisions.set_state(revision.id, state)
        revision = self.revisions.get(revision.id)
        report = self._report(revision, {})
        return {
            "finalized": True,
            "state": state,
            "revision": revision.to_dict(),
            "pending": partial,
            "report": report,
        }

    def deliverable_path(self, revision: Revision) -> Path:
        return self.revisions.path(revision)

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


def _page_count(previews: dict[str, Any], prior: dict[str, Any]) -> int:
    return max(len(previews["pages"]), int(prior.get("page_count") or 0))


def _edit_catalog() -> dict[str, str]:
    return {
        "pptx.set_text": "slide|slide_id, shape_id|shape_name, text, expected_text?",
        "pptx.replace_text": "find, replace, slides?, expected_count?, include_notes?",
        "pptx.set_table_cell": "slide, shape, row, col (0-based), text, expected_text?",
        "pptx.update_chart": (
            "slide, shape, categories, series[{name, values}], number_format?, expected_categories?"
        ),
        "pptx.replace_image": "slide, shape, image (resource key), fit? cover|contain",
        "pptx.set_notes": "slide, text, expected_text?",
        "pptx.delete_slide": "slide, expected_title?",
        "pptx.move_slide": "slide, to",
        "pptx.add_slide": "after (0 = first), spec (a DeckSpec slide), theme?",
    }
