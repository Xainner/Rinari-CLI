"""Operaciones cerradas del runtime documental.

`REGISTRY` son las que corren en el proceso hijo (worker.py): reciben una
solicitud validada y un directorio de salida, y devuelven `(resultado,
archivos)`. `INSPECT` y `READ` son lecturas acotadas que corren en el Engine
tras la inspección del contenedor. Importar este módulo marca como
disponibles las capacidades que implementa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.documents import capabilities
from rinari.documents.contracts import DocumentError, DocumentErrorCode

Files = dict[str, bytes]


def _render(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import render

    source = Path(request["path"])
    kind = request["kind"]
    pdf, backend = render.to_pdf(source, kind, out)
    count = render.pdf_page_count(pdf)
    pages = request.get("pages") or list(
        range(1, min(count, int(request.get("max_pages") or count)) + 1)
    )
    images = render.pdf_to_png(pdf, pages, max_side=int(request.get("max_side") or 1600))
    files: Files = {"pdf": pdf}
    rows = []
    for image in images:
        files[f"page-{image['page']}"] = image["png"]
        rows.append({"page": image["page"], "width": image["width"], "height": image["height"]})
    return {
        "backend": backend,
        "page_count": count,
        "pages": rows,
    }, files


def _checks(path: Path, expected: list[str] | None = None) -> dict[str, Any]:
    from rinari.documents.validation import pptx_checks

    return {
        "structure": pptx_checks.structure(path).to_dict(),
        "layout": pptx_checks.layout(path).to_dict(),
        "content": pptx_checks.content(path, expected).to_dict(),
    }


def _pptx_create(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import pptx_build

    data, plans = pptx_build.build(request["spec"], request.get("resources") or {})
    target = out / "deck.pptx"
    target.write_bytes(data)
    return {
        "slides": [
            {"id": plan.id, "layout": plan.layout, "elements": len(plan.elements)} for plan in plans
        ],
        "plan_findings": pptx_build.plan_findings(plans),
        "checks": _checks(target, request.get("expected_text")),
    }, {"document": data}


def _pptx_edit(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation
    from rinari.documents.adapters import pptx_edit

    source = Path(request["path"])
    target = out / "edited.pptx"
    state = pptx_edit.apply(source, target, request["operations"], request.get("resources") or {})
    diff = preservation.diff_packages(source, target)
    verdict = preservation.evaluate(
        diff,
        state.allowed,
        request.get("preservation") or preservation.PRESERVE_STRICT,
        allow_new=tuple(sorted(state.allow_new)),
    )
    if verdict["status"] == "failed":
        raise DocumentError(
            DocumentErrorCode.PRESERVATION_RISK,
            "The edit would change parts it did not declare",
            details={"preservation": verdict},
            action="Use a narrower operation or allow a rebuild explicitly",
        )
    return {
        "changes": state.changes,
        "plan_findings": state.findings,
        "preservation": {**verdict, "diff": diff},
        "semantic_diff": preservation.semantic_diff(source, target),
        "checks": _checks(target, request.get("expected_text")),
    }, {"document": target.read_bytes()}


def _pptx_validate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    return {"checks": _checks(Path(request["path"]), request.get("expected_text"))}, {}


def _diff(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation

    before, after = Path(request["before"]), Path(request["after"])
    result: dict[str, Any] = {"package": preservation.diff_packages(before, after)}
    if request.get("kind") == "pptx":
        result["content"] = preservation.semantic_diff(before, after)
    return result, {}


REGISTRY: dict[str, Any] = {
    "render": _render,
    "pptx.create": _pptx_create,
    "pptx.edit": _pptx_edit,
    "pptx.validate": _pptx_validate,
    "diff": _diff,
}


def _inspect_pptx(path: Path) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.inspect(path)


def _read_pptx(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.read(path, selection, cursor)


def _basic(name: str):
    def call(*args):
        from rinari.documents.adapters import basic_read

        return getattr(basic_read, name)(*args)

    return call


INSPECT: dict[str, Any] = {
    "pptx": _inspect_pptx,
    "pdf": _basic("inspect_pdf"),
    "docx": _basic("inspect_docx"),
    "xlsx": _basic("inspect_xlsx"),
}
READ: dict[str, Any] = {
    "pptx": _read_pptx,
    "pdf": _basic("read_pdf"),
    "docx": _basic("read_docx"),
    "xlsx": _basic("read_xlsx"),
}

for _kind in INSPECT:
    capabilities.enable(_kind, "inspect")
for _kind in READ:
    capabilities.enable(_kind, "read")
capabilities.enable("pdf", "render")
capabilities.enable("pptx", "create")
capabilities.enable("pptx", "edit")


def register(name: str, function: Any, *, kind: str | None = None, operation: str | None = None):
    REGISTRY[name] = function
    if kind and operation:
        capabilities.enable(kind, operation)


def inspect(kind: str, path: Path) -> dict[str, Any]:
    function = INSPECT.get(kind)
    if function is None:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot inspect {kind} yet")
    return function(path)


def read(kind: str, path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    function = READ.get(kind)
    if function is None:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot read {kind} yet")
    return function(path, selection, cursor)
