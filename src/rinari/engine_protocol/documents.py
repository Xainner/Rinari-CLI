"""Operaciones documentales para el escritorio, sobre el mismo DocumentService.

El protocolo y las herramientas del modelo son superficies distintas sobre un
servicio común. `documents.job.start` solo acepta operaciones cerradas; una
mutación iniciada desde el escritorio pasa por el mismo servicio y sus
reglas. El progreso llega por `document.job.updated`; tras reconectar, el
cliente reconcilia con `documents.job.get`.
"""

from __future__ import annotations

from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

JOB_OPERATIONS = ("render",)


def _text(params: dict[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = params.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise EngineProtocolError(INVALID_PARAMS, f"Param {key!r} must be a non-empty string.")
    return value


def _pages(params: dict[str, Any]) -> list[int] | None:
    value = params.get("pages")
    if value is None:
        return None
    if not isinstance(value, list) or not all(type(v) is int and v > 0 for v in value):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'pages' must be a list of page numbers.")
    return value[:200]


def register_documents(dispatcher, services, emit, resolve_file=None) -> None:
    from rinari.documents.contracts import DocumentError
    from rinari.documents.jobs import JobManager
    from rinari.documents.service import DocumentService

    manager = JobManager.for_context(services.ctx)
    manager.add_listener(emit)

    def service(params: dict[str, Any]) -> DocumentService:
        session_id = _text(params, "session_id")
        services.sessions.show(session_id)
        return DocumentService(services.artifacts, session_id)

    def guarded(function):
        def call(params: dict[str, Any]) -> dict[str, Any]:
            try:
                return function(params)
            except DocumentError as exc:
                raise EngineProtocolError(
                    exc.code.value, exc.message, details=exc.to_dict()
                ) from exc

        return call

    def import_file(params):
        """Un archivo del workspace, con la misma autorización que `workspace.file.*`.

        Se copia al Artifact Store y pasa a ser la revisión 0; el mismo
        contenido vuelve a la misma revisión, uno cambiado es otra.
        """
        if resolve_file is None:
            raise EngineProtocolError(INVALID_PARAMS, "Workspace files are not available here.")
        resolved = resolve_file(params)
        documents = service(params)
        return {"revision": documents.import_path(resolved.path).to_dict()}

    def capabilities(params):
        from rinari.documents.capabilities import capabilities as caps

        return {"capabilities": caps(params.get("kind"), params.get("operation"))}

    def inspect(params):
        return service(params).inspect(_text(params, "ref"))

    def job_start(params):
        operation = _text(params, "operation")
        if operation not in JOB_OPERATIONS:
            raise EngineProtocolError(INVALID_PARAMS, f"Unknown document operation {operation!r}.")
        return service(params).render(_text(params, "ref"), _pages(params))

    def job_get(params):
        return service(params).job(_text(params, "job_id"))

    def job_cancel(params):
        return service(params).cancel(_text(params, "job_id"))

    def preview_get(params):
        documents = service(params)
        revision = documents.resolve(_text(params, "ref"))
        return {
            "revision": revision.to_dict(),
            "preview": documents.previews(revision),
        }

    def range_get(params):
        cursor = params.get("cursor")
        if cursor is not None and (type(cursor) is not int or cursor < 1):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'cursor' must be a positive int.")
        return service(params).read(
            _text(params, "ref"), _text(params, "selection", optional=True), cursor
        )

    def report_get(params):
        documents = service(params)
        revision = documents.revisions.get(
            _text(params, "revision_id"), session_id=documents.session_id
        )
        return {"revision_id": revision.id, "report": revision.report}

    def revisions_list(params):
        documents = service(params)
        return {"revisions": documents.list_revisions(_text(params, "document_id", optional=True))}

    dispatcher.register("documents.capabilities.get", guarded(capabilities))
    dispatcher.register("documents.import", guarded(import_file))
    dispatcher.register("documents.inspect", guarded(inspect))
    dispatcher.register("documents.job.start", guarded(job_start))
    dispatcher.register("documents.job.get", guarded(job_get))
    dispatcher.register("documents.job.cancel", guarded(job_cancel))
    dispatcher.register("documents.preview.get", guarded(preview_get))
    dispatcher.register("documents.range.get", guarded(range_get))
    dispatcher.register("documents.report.get", guarded(report_get))
    dispatcher.register("documents.revisions.list", guarded(revisions_list))
