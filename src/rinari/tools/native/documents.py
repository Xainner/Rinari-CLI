"""Herramientas documentales del modelo sobre DocumentService.

Son perezosas (`always_loaded=False`): no pesan en cada petición; las activa
una skill documental (sus `required_tools`) o `capability.search`. Una
referencia de documento es un `artifact://` de la sesión, una revisión
(`rev_…`) o una ruta local autorizada, que se importa antes de tocarla.
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
    return _job_result(service, job, show)


def documents_job_cancel(arguments: dict, ctx: ToolContext) -> ToolResult:
    try:
        job = _service(ctx).cancel(str(arguments.get("job_id") or ""))
    except Exception as exc:
        return _fail(exc)
    return ToolResult(ok=True, data=job)


def _read_target(arguments: dict) -> ClassifiedAction:
    ref = str(arguments.get("document") or "")
    if ref.startswith(("artifact://", "rev_")):
        return ClassifiedAction("state.read")
    return ClassifiedAction("fs.read", ref)


_DOCUMENT = {
    "type": "string",
    "description": "artifact:// URI, revision id (rev_…) or a local file path.",
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
