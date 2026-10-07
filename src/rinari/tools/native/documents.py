"""Herramientas documentales del modelo sobre DocumentService.

Son perezosas (`always_loaded=False`): no pesan en cada petición; las activa
una skill documental (sus `required_tools`) o `capability.search`. Una
referencia de documento es un `artifact://` de la sesión, una revisión
(`rev_…`) o una ruta local autorizada, que se importa antes de tocarla.

Crear y editar producen revisiones nuevas en el Artifact Store: el original
no se toca. Solo `documents.finalize` con `save_to` escribe en el proyecto, y
nunca encima de un archivo existente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ArtifactRef,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

MAX_WAIT_S = 600
DEFAULT_WAIT_S = 120
MAX_SHOWN_PAGES = 6

_ERROR_MAP = {
    "NOT_FOUND": ToolErrorCode.NOT_FOUND,
    "INVALID_SPEC": ToolErrorCode.INVALID_ARGUMENT,
    "REVISION_CONFLICT": ToolErrorCode.CONFLICT,
    "CANCELLED": ToolErrorCode.CANCELLED,
    "BACKEND_UNAVAILABLE": ToolErrorCode.DEPENDENCY_ERROR,
    "CALCULATION_BACKEND_UNAVAILABLE": ToolErrorCode.DEPENDENCY_ERROR,
    "LICENSE_REQUIRED": ToolErrorCode.DEPENDENCY_ERROR,
    "UNSAFE_EXTERNAL_RESOURCE": ToolErrorCode.POLICY_DENIED,
}


def _fail(error) -> ToolResult:
    from rinari.documents.contracts import DocumentError

    if isinstance(error, DocumentError):
        code = _ERROR_MAP.get(error.code.value, ToolErrorCode.INVALID_ARGUMENT)
        message = error.message + (f" ({error.action})" if error.action else "")
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=code,
                message=f"{error.code.value}: {message}",
                retryable=error.retryable,
                details=error.to_dict(),
            ),
        )
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=ToolErrorCode.INVALID_ARGUMENT, message=str(error))
    )


def _service(ctx: ToolContext):
    from rinari.documents.contracts import DocumentError, DocumentErrorCode
    from rinari.documents.service import DocumentService

    if ctx.artifact_store is None:
        raise DocumentError(DocumentErrorCode.BACKEND_UNAVAILABLE, "The artifact store is off")
    return DocumentService(ctx.artifact_store, ctx.session_id)


def _reference(service, ctx: ToolContext, ref: Any) -> str:
    """Una ruta local se importa (con permiso de lectura) y pasa a ser revisión."""
    from rinari.documents.contracts import DocumentError, DocumentErrorCode

    if not isinstance(ref, str) or not ref.strip():
        raise DocumentError(DocumentErrorCode.INVALID_SPEC, "document is required")
    if ref.startswith(("artifact://", "rev_")):
        return ref
    try:
        path = ctx.sandbox.resolve(ref, base=ctx.cwd)
        ctx.sandbox.assert_readable(path)
    except Exception as exc:
        raise DocumentError(
            DocumentErrorCode.UNSAFE_EXTERNAL_RESOURCE, getattr(exc, "message", str(exc))
        ) from exc
    if not Path(path).is_file():
        raise DocumentError(DocumentErrorCode.NOT_FOUND, f"No such file: {ref}")
    return service.import_path(Path(path)).id


def _wait(arguments: dict, default: int = DEFAULT_WAIT_S) -> float:
    value = arguments.get("wait_s", default)
    return float(min(MAX_WAIT_S, max(0, value if isinstance(value, (int, float)) else default)))


def documents_capabilities(arguments: dict, ctx: ToolContext) -> ToolResult:
    from rinari.documents import capabilities

    rows = capabilities.capabilities(arguments.get("kind"), arguments.get("operation"))
    return ToolResult(ok=True, data={"capabilities": rows})


def documents_inspect(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        data = service.inspect(_reference(service, ctx, arguments.get("document")))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def documents_read(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        ref = _reference(service, ctx, arguments.get("document"))
        data = service.read(ref, arguments.get("selection"), arguments.get("cursor"))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def _job_result(service, job: dict[str, Any], show: list[int] | None) -> ToolResult:
    """Un trabajo aceptado es ok; su `status` dice si terminó. Puede adjuntar páginas."""
    images = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    result = job.get("result") or {}
    pages = result.get("pages") or []
    if pages:
        artifacts = tuple(ArtifactRef(p["uri"], f"p. {p['page']}", "image") for p in pages)
    if show and pages:
        from rinari.models.images import references

        wanted = [p for p in pages if p["page"] in set(show)][:MAX_SHOWN_PAGES]
        records = [service.artifacts.meta(p["uri"]) for p in wanted]
        images = references(
            service.artifacts,
            service.session_id,
            [{"uri": r.uri(), "sha256": r.sha256} for r in records],
        )
    return ToolResult(ok=True, data=job, images=images, artifacts=artifacts)


def _pages(value: Any) -> list[int] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return [int(v) for v in value]
    from rinari.documents.adapters.pptx_read import parse_selection

    return parse_selection(str(value), 100_000)


def documents_render(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        ref = _reference(service, ctx, arguments.get("document"))
        pages = _pages(arguments.get("pages"))
        job = service.render(ref, pages)
        job = service.wait(job, _wait(arguments), ctx.cancellation)
        show = _pages(arguments.get("show"))
    except Exception as exc:
        return _fail(exc)
    return _job_result(service, job, show)


def documents_job_get(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        job = service.job(str(arguments.get("job_id") or ""))
        if arguments.get("wait_s"):
            job = service.wait(job, _wait(arguments), ctx.cancellation)
        show = _pages(arguments.get("show"))
    except Exception as exc:
        return _fail(exc)
    if job.get("operation") in ("create", "edit"):
        return _built(service, job, show)
    return _job_result(service, job, show)


def documents_job_cancel(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        job = _service(ctx).cancel(str(arguments.get("job_id") or ""))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=job)


def _resources(service, ctx: ToolContext, refs: Any) -> dict[str, str] | None:
    """Imágenes: `artifact://` de la sesión o rutas legibles, que se importan."""
    if not refs:
        return None
    if not isinstance(refs, dict):
        from rinari.documents.contracts import DocumentError, DocumentErrorCode

        raise DocumentError(DocumentErrorCode.INVALID_SPEC, "resources is a name -> image map")
    from rinari.artifacts.transfer import import_file
    from rinari.documents.contracts import DocumentError, DocumentErrorCode

    out: dict[str, str] = {}
    for key, ref in refs.items():
        if isinstance(ref, str) and ref.startswith("artifact://"):
            out[str(key)] = ref
            continue
        try:
            path = ctx.sandbox.resolve(str(ref), base=ctx.cwd)
            ctx.sandbox.assert_readable(path)
        except Exception as exc:
            raise DocumentError(
                DocumentErrorCode.UNSAFE_EXTERNAL_RESOURCE, getattr(exc, "message", str(exc))
            ) from exc
        if not Path(path).is_file():
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"No such file: {ref}")
        record = import_file(
            service.artifacts, service.session_id, Path(path), provenance=f"local:{path}"
        )
        out[str(key)] = record.uri()
    return out


def _texts(value: Any) -> list[str] | None:
    if not value:
        return None
    return [str(v) for v in value][:200]


def documents_templates(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        data = _service(ctx).templates(arguments.get("kind"))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def _spec(arguments: dict, ctx: ToolContext) -> Any:
    """El spec en línea o un `artifact://` JSON de la sesión (para decks grandes)."""
    spec = arguments.get("spec")
    uri = arguments.get("spec_uri")
    if spec is None and isinstance(uri, str):
        import json

        from rinari.documents.contracts import DocumentError, DocumentErrorCode

        service = _service(ctx)
        if not uri.startswith(f"artifact://{service.session_id}/"):
            raise DocumentError(DocumentErrorCode.NOT_FOUND, "spec_uri must be of this session")
        try:
            spec = json.loads(service.artifacts.get(uri).decode("utf-8"))
        except Exception as exc:
            raise DocumentError(
                DocumentErrorCode.INVALID_SPEC, f"spec_uri is not a JSON spec: {exc}"
            ) from exc
    return spec


def _built(service, job: dict[str, Any], show: list[int] | None) -> ToolResult:
    """El trabajo de crear/editar; sus páginas renderizadas pueden ir adjuntas."""
    render = (job.get("result") or {}).get("render") or {}
    if not render.get("pages"):
        return ToolResult(ok=True, data=job)
    pages = _job_result(service, {**job, "result": render}, show)
    return ToolResult(ok=True, data=job, images=pages.images, artifacts=pages.artifacts)


def documents_create(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        job = service.create(
            _spec(arguments, ctx),
            output_name=arguments.get("output_name"),
            resources=_resources(service, ctx, arguments.get("resources")),
            render=arguments.get("render", True) is not False,
            expected_text=_texts(arguments.get("expected_text")),
        )
        job = service.wait(job, _wait(arguments), ctx.cancellation)
        show = _pages(arguments.get("show"))
    except Exception as exc:
        return _fail(exc)
    return _built(service, job, show)


def documents_edit(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        ref = _reference(service, ctx, arguments.get("document"))
        job = service.edit(
            ref,
            arguments.get("operations"),
            expected_sha256=arguments.get("expected_sha256"),
            preservation=arguments.get("preservation") or "preserve_strict",
            output_name=arguments.get("output_name"),
            resources=_resources(service, ctx, arguments.get("resources")),
            render=arguments.get("render", True) is not False,
            expected_text=_texts(arguments.get("expected_text")),
        )
        job = service.wait(job, _wait(arguments), ctx.cancellation)
        show = _pages(arguments.get("show"))
    except Exception as exc:
        return _fail(exc)
    return _built(service, job, show)


def documents_validate(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        ref = _reference(service, ctx, arguments.get("document"))
        data = service.validate(ref, _texts(arguments.get("expected_text")))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def documents_review(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        data = service.review(
            str(arguments.get("document") or ""),
            pages=_pages(arguments.get("pages")),
            findings=arguments.get("findings"),
            reviewer=arguments.get("reviewer"),
        )
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def documents_diff(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        service = _service(ctx)
        before = arguments.get("before")
        after = str(arguments.get("after") or "")
        if not before:
            parent = service.resolve(after).parent_id
            if parent is None:
                from rinari.documents.contracts import DocumentError, DocumentErrorCode

                raise DocumentError(
                    DocumentErrorCode.INVALID_SPEC, "before is required (no parent revision)"
                )
            before = parent
        data = service.diff(str(before), after)
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=data)


def documents_finalize(arguments: dict, ctx: ToolContext) -> ToolResult:
    import shutil

    from rinari.tools.native.artifact import _free_path

    try:
        service = _service(ctx)
        data = service.finalize(
            str(arguments.get("document") or ""),
            accept_partial=bool(arguments.get("accept_partial")),
        )
    except Exception as exc:
        return _fail(exc)
    target = arguments.get("save_to")
    if not data["finalized"] or not target:
        artifacts = ()
        if data["finalized"]:
            revision = data["revision"]
            artifacts = (ArtifactRef(revision["uri"], revision["name"], "document"),)
        return ToolResult(ok=True, data=data, artifacts=artifacts)
    try:
        dest = ctx.sandbox.resolve(str(target), base=ctx.cwd)
        if dest.is_dir():
            dest = dest / data["revision"]["name"]
        ctx.sandbox.assert_writable(dest)
    except Exception as exc:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.SANDBOX_VIOLATION, message=getattr(exc, "message", str(exc))
            ),
        )
    try:
        source = service.deliverable_path(service.resolve(data["revision"]["id"]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = _free_path(dest)
        shutil.copyfile(source, dest)
    except Exception as exc:
        return _fail(exc)
    revision = data["revision"]
    return ToolResult(
        ok=True,
        data={**data, "saved_to": str(dest)},
        artifacts=(ArtifactRef(revision["uri"], revision["name"], "document"),),
    )


def _write_target(arguments: dict) -> ClassifiedAction:
    target = arguments.get("save_to")
    if target:
        return ClassifiedAction("fs.write", str(target))
    return ClassifiedAction("state.read")


def _read_target(arguments: dict) -> ClassifiedAction:
    ref = str(arguments.get("document") or "")
    if ref.startswith(("artifact://", "rev_")):
        return ClassifiedAction("state.read")
    return ClassifiedAction("fs.read", ref)


_DOCUMENT = {
    "type": "string",
    "description": "artifact:// URI, revision id (rev_…) or a local file path.",
}
_REVISION = {"type": "string", "description": "Revision id (rev_…)."}
_RESOURCES = {
    "type": "object",
    "description": "Images by name: artifact:// URI or local path.",
    "additionalProperties": {"type": "string"},
}
_TEXTS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Text that must appear (content check).",
}
_WAIT = {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_S}
_PAGES = {"type": "string", "description": "e.g. '1-3,7'"}


def document_tools() -> list[ToolDefinition]:
    common = {"namespace": "documents", "always_loaded": False}
    return [
        ToolDefinition(
            name="documents.capabilities",
            description=(
                "What this installation can do with pptx/xlsx/docx/pdf: inspect, read, create, "
                "edit, render, calculate, query, redact; with the backend or why not."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["pptx", "xlsx", "docx", "pdf"]},
                    "operation": {"type": "string"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_capabilities,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.inspect",
            description=(
                "Inventory of a document: structure, features, risks (macros, links, "
                "unsupported objects) and limits. Do this before editing."
            ),
            input_schema={
                "type": "object",
                "properties": {"document": _DOCUMENT},
                "required": ["document"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_inspect,
            classify=_read_target,
            **common,
        ),
        ToolDefinition(
            name="documents.read",
            description=(
                "Content of selected slides/pages/sheets with stable ids for edits. "
                "Continue with next_cursor when truncated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _DOCUMENT,
                    "selection": {"type": "string", "description": "e.g. '1-3,7'"},
                    "cursor": {"type": "integer", "minimum": 1},
                },
                "required": ["document"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_read,
            classify=_read_target,
            **common,
        ),
        ToolDefinition(
            name="documents.render",
            description=(
                "Render a revision to page/slide images (cached per revision). show attaches "
                f"up to {MAX_SHOWN_PAGES} pages for visual review. Waits up to wait_s."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _DOCUMENT,
                    "pages": _PAGES,
                    "show": _PAGES,
                    "wait_s": _WAIT,
                },
                "required": ["document"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            timeout_ms=MAX_WAIT_S * 1000 + 30_000,
            handler=documents_render,
            classify=_read_target,
            **common,
        ),
        ToolDefinition(
            name="documents.templates",
            description=(
                "Themes, slide layouts (with their fields) and typed edit operations. "
                "Read before documents.create or documents.edit."
            ),
            input_schema={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["pptx"]}},
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_templates,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.create",
            description=(
                "Build a new editable document from a spec (pptx: DeckSpec with theme and "
                "slides). Returns a draft revision, static checks and renders; show attaches "
                "pages. Nothing is written to the project."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spec": {"type": "object"},
                    "spec_uri": {"type": "string"},
                    "output_name": {"type": "string"},
                    "resources": _RESOURCES,
                    "expected_text": _TEXTS,
                    "render": {"type": "boolean"},
                    "show": _PAGES,
                    "wait_s": _WAIT,
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            timeout_ms=MAX_WAIT_S * 1000 + 30_000,
            handler=documents_create,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.edit",
            description=(
                "Apply typed operations (see documents.templates) to a document. Produces a "
                "new revision; the original is untouched. preserve_strict refuses changes "
                "to parts the operations did not declare."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _DOCUMENT,
                    "operations": {"type": "array", "items": {"type": "object"}},
                    "expected_sha256": {"type": "string"},
                    "preservation": {
                        "type": "string",
                        "enum": ["preserve_strict", "preserve_best_effort", "rebuild"],
                    },
                    "output_name": {"type": "string"},
                    "resources": _RESOURCES,
                    "expected_text": _TEXTS,
                    "render": {"type": "boolean"},
                    "show": _PAGES,
                    "wait_s": _WAIT,
                },
                "required": ["document", "operations"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            timeout_ms=MAX_WAIT_S * 1000 + 30_000,
            handler=documents_edit,
            classify=_read_target,
            **common,
        ),
        ToolDefinition(
            name="documents.validate",
            description=(
                "Checks of a revision by dimension (structure, layout, content, "
                "preservation, visual) with evidence. Visual is passed only after "
                "documents.review covers every page."
            ),
            input_schema={
                "type": "object",
                "properties": {"document": _DOCUMENT, "expected_text": _TEXTS},
                "required": ["document"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_validate,
            classify=_read_target,
            **common,
        ),
        ToolDefinition(
            name="documents.review",
            description=(
                "Record the visual review you did of this revision's renders: pages you "
                "looked at and findings (severity critical|error|warning|suggestion, page, "
                "message, fix)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _REVISION,
                    "pages": _PAGES,
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "reviewer": {"type": "string"},
                },
                "required": ["document", "pages"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_review,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.diff",
            description=(
                "What changed between two revisions: content by slide/shape and OOXML "
                "parts. before defaults to the parent revision."
            ),
            input_schema={
                "type": "object",
                "properties": {"before": _REVISION, "after": _REVISION},
                "required": ["after"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_diff,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.finalize",
            description=(
                "Deliver a revision whose checks pass (or, with accept_partial, label it an "
                "accepted draft). Otherwise returns what blocks it. save_to copies it into "
                "the project without overwriting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": _REVISION,
                    "accept_partial": {"type": "boolean"},
                    "save_to": {"type": "string"},
                },
                "required": ["document"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            handler=documents_finalize,
            classify=_write_target,
            **common,
        ),
        ToolDefinition(
            name="documents.job.get",
            description="State of a document job; wait_s waits for it. show attaches pages.",
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}, "wait_s": _WAIT, "show": _PAGES},
                "required": ["job_id"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            timeout_ms=MAX_WAIT_S * 1000 + 30_000,
            handler=documents_job_get,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
        ToolDefinition(
            name="documents.job.cancel",
            description="Cancel a running document job of this session.",
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=documents_job_cancel,
            classify=lambda _: ClassifiedAction("state.read"),
            **common,
        ),
    ]


__all__ = ["document_tools"]
