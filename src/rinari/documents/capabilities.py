"""Qué puede hacer de verdad esta instalación con cada formato.

Nada de un `supports_office` global: leer un XLSX no es calcularlo, escribir
un PPTX no es renderizarlo. Cada (formato, operación) dice si está disponible,
con qué backend y, si no, por qué y qué haría falta.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from rinari.documents.adapters import render

OPERATIONS = ("inspect", "read", "create", "edit", "render", "calculate", "query", "redact")

# Implementado en este Engine: (formato, operación) -> (backend, módulo requerido)
_IMPLEMENTED: dict[tuple[str, str], tuple[str, str | None]] = {
    ("pptx", "inspect"): ("python-pptx", "pptx"),
    ("pptx", "read"): ("python-pptx", "pptx"),
    ("pptx", "create"): ("python-pptx", "pptx"),
    ("pptx", "edit"): ("python-pptx", "pptx"),
    ("xlsx", "inspect"): ("openpyxl", "openpyxl"),
    ("xlsx", "read"): ("openpyxl", "openpyxl"),
    ("xlsx", "create"): ("xlsxwriter", "xlsxwriter"),
    ("xlsx", "edit"): ("openpyxl", "openpyxl"),
    ("xlsx", "query"): ("duckdb", "duckdb"),
    ("docx", "inspect"): ("python-docx", "docx"),
    ("docx", "read"): ("python-docx", "docx"),
    ("docx", "create"): ("python-docx", "docx"),
    ("docx", "edit"): ("python-docx", "docx"),
    ("pdf", "inspect"): ("pypdf", "pypdf"),
    ("pdf", "read"): ("pypdfium2", "pypdfium2"),
    ("pdf", "create"): ("reportlab", "reportlab"),
    ("pdf", "edit"): ("pypdf", "pypdf"),
    ("pdf", "render"): ("pdfium", "pypdfium2"),
}

_ENABLED: set[tuple[str, str]] = set()


def enable(kind: str, operation: str) -> None:
    """Un adaptador se declara listo al importarse (ver documents/operations.py)."""
    _ENABLED.add((kind, operation))


def _module(name: str | None) -> bool:
    return name is None or importlib.util.find_spec(name) is not None


def capability(kind: str, operation: str) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": kind, "operation": operation}
    if operation == "render" and kind in ("pptx", "docx", "xlsx"):
        backends = render.office_renderers(kind)
        row.update(
            available=bool(backends),
            backend=backends[0] if backends else None,
            backends=backends,
        )
        if not backends:
            row["reason"] = "BACKEND_UNAVAILABLE"
            row["action"] = "Install Microsoft Office or LibreOffice to render previews"
        return row
    if operation == "calculate":
        if kind != "xlsx":
            return {**row, "available": False, "reason": "UNSUPPORTED_FEATURE"}
        excel = render.office_apps().get("xlsx", False) and ("xlsx", "calculate") in _ENABLED
        row.update(available=excel, backend="excel-com" if excel else None)
        if not excel:
            row["reason"] = "CALCULATION_BACKEND_UNAVAILABLE"
            row["action"] = (
                "Formulas are written and kept; their results stay pending without Excel"
            )
        return row
    if operation == "redact":
        row.update(available=("pdf", "redact") in _ENABLED and kind == "pdf")
        if row["available"]:
            row["backend"] = "raster-pdfium"
            row["note"] = "Affected pages become images and lose selectable text"
        else:
            row["reason"] = "BACKEND_UNAVAILABLE"
            row["action"] = "Secure redaction needs a certified backend; it is never simulated"
        return row
    implemented = _IMPLEMENTED.get((kind, operation))
    if implemented is None:
        return {**row, "available": False, "reason": "UNSUPPORTED_FEATURE"}
    backend, module = implemented
    ready = (kind, operation) in _ENABLED and _module(module)
    row.update(available=ready, backend=backend if ready else None)
    if not ready:
        row["reason"] = "BACKEND_UNAVAILABLE" if not _module(module) else "UNSUPPORTED_FEATURE"
    return row


def capabilities(kind: str | None = None, operation: str | None = None) -> list[dict[str, Any]]:
    from rinari.documents import operations  # noqa: F401  (registra adaptadores)

    kinds = [kind] if kind else ["pptx", "xlsx", "docx", "pdf"]
    ops = [operation] if operation else list(OPERATIONS)
    return [capability(k, o) for k in kinds for o in ops]


def require(kind: str, operation: str) -> dict[str, Any]:
    from rinari.documents.contracts import DocumentError, DocumentErrorCode

    row = capability(kind, operation)
    if not row.get("available"):
        code = DocumentErrorCode(row.get("reason") or "UNSUPPORTED_FEATURE")
        raise DocumentError(
            code,
            f"{operation} is not available for {kind} here",
            action=row.get("action"),
        )
    return row
