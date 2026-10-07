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


REGISTRY: dict[str, Any] = {"render": _render}


def _inspect_pptx(path: Path) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.inspect(path)


def _read_pptx(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.read(path, selection, cursor)


INSPECT: dict[str, Any] = {"pptx": _inspect_pptx}
READ: dict[str, Any] = {"pptx": _read_pptx}

for _kind in INSPECT:
    capabilities.enable(_kind, "inspect")
for _kind in READ:
    capabilities.enable(_kind, "read")
capabilities.enable("pdf", "render")


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
