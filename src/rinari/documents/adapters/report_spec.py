"""ReportSpec: un informe descrito por bloques, compilable a DOCX y a PDF.

El mismo spec produce el Word editable y el PDF de lectura: no hay dos
documentos que acaben con cifras distintas. Esquema cerrado; el texto admite
solo `**negrita**` y `*cursiva*` en línea.
"""

from __future__ import annotations

import re
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

MAX_BLOCKS = 4_000
MAX_TABLE_ROWS = 5_000
BLOCK_TYPES = (
    "heading",
    "paragraph",
    "bullets",
    "table",
    "image",
    "quote",
    "callout",
    "kpis",
    "page_break",
    "appendix",
)
REPORT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "title",
        "subtitle",
        "author",
        "date",
        "language",
        "theme",
        "page",
        "cover",
        "toc",
        "header",
        "footer",
        "blocks",
    }
)
BLOCK_KEYS: dict[str, frozenset[str]] = {
    "heading": frozenset({"type", "text", "level"}),
    "paragraph": frozenset({"type", "text", "style"}),
    "bullets": frozenset({"type", "items", "numbered"}),
    "table": frozenset({"type", "columns", "rows", "caption", "widths", "highlight_row", "source"}),
    "image": frozenset({"type", "image", "width_cm", "caption"}),
    "quote": frozenset({"type", "text", "author"}),
    "callout": frozenset({"type", "title", "text", "tone"}),
    "kpis": frozenset({"type", "items"}),
    "page_break": frozenset({"type"}),
    "appendix": frozenset({"type", "title"}),
}
PAGE_KEYS = frozenset({"size", "orientation", "margins_cm"})
PAGE_SIZES = {"A4": (21.0, 29.7), "Letter": (21.59, 27.94)}
TONES = ("info", "warning", "success")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")


def _invalid(message: str, **details: Any) -> DocumentError:
    return DocumentError(DocumentErrorCode.INVALID_SPEC, message, details=details)


def validate(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise _invalid("spec must be an object")
    unknown = sorted(set(spec) - REPORT_KEYS)
    if unknown:
        raise _invalid(f"Unknown report fields: {', '.join(unknown)}", allowed=sorted(REPORT_KEYS))
    page = spec.get("page") or {}
    if not isinstance(page, dict) or set(page) - PAGE_KEYS:
        raise _invalid("page is {size: A4|Letter, orientation, margins_cm}")
    if page.get("size", "A4") not in PAGE_SIZES:
        raise _invalid("page.size must be A4 or Letter")
    if page.get("orientation", "portrait") not in ("portrait", "landscape"):
        raise _invalid("page.orientation must be portrait or landscape")
    margins = page.get("margins_cm", 2.5)
    if not isinstance(margins, (int, float)) or not 1 <= margins <= 5:
        raise _invalid("page.margins_cm must be 1-5")
    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise _invalid("blocks must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise DocumentError(
            DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, f"At most {MAX_BLOCKS} blocks"
        )
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict) or block.get("type") not in BLOCK_KEYS:
            raise _invalid(f"block {index}: type must be one of {', '.join(BLOCK_TYPES)}")
        kind = block["type"]
        extra = sorted(set(block) - BLOCK_KEYS[kind])
        if extra:
            raise _invalid(
                f"block {index} ({kind}): unknown fields {', '.join(extra)}",
                allowed=sorted(BLOCK_KEYS[kind]),
            )
        if (
            kind in ("heading", "paragraph", "quote", "callout")
            and not str(block.get("text") or "").strip()
        ):
            raise _invalid(f"block {index} ({kind}): text is required")
        if kind == "heading" and block.get("level", 1) not in (1, 2, 3):
            raise _invalid(f"block {index}: heading level is 1-3")
        if kind == "bullets":
            items = block.get("items")
            if not isinstance(items, list) or not items:
                raise _invalid(f"block {index}: bullets needs items")
        if kind == "table":
            columns, rows = block.get("columns"), block.get("rows")
            if not isinstance(columns, list) or not columns or not isinstance(rows, list):
                raise _invalid(f"block {index}: table needs columns and rows")
            if len(rows) > MAX_TABLE_ROWS:
                raise DocumentError(
                    DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED,
                    f"block {index}: at most {MAX_TABLE_ROWS} rows per table",
                )
            for row in rows:
                if not isinstance(row, list) or len(row) > len(columns):
                    raise _invalid(f"block {index}: each row is a list no longer than columns")
        if kind == "image" and not block.get("image"):
            raise _invalid(f"block {index}: image needs a resource name")
        if kind == "callout" and block.get("tone", "info") not in TONES:
            raise _invalid(f"block {index}: tone is info|warning|success")
        if kind == "kpis":
            items = block.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 4:
                raise _invalid(f"block {index}: kpis needs 1-4 items {{label, value, delta?}}")
        if kind == "appendix" and not block.get("title"):
            raise _invalid(f"block {index}: appendix needs a title")
    return spec


def runs(text: str) -> list[tuple[str, bool, bool]]:
    """Texto con `**negrita**` y `*cursiva*` → [(texto, negrita, cursiva)]."""
    out: list[tuple[str, bool, bool]] = []
    for part in _INLINE.split(str(text)):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True, False))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], False, True))
        else:
            out.append((part, False, False))
    return out


def plain(text: str) -> str:
    return "".join(piece for piece, _, _ in runs(text))


LABELS = {
    "es": {
        "table": "Tabla",
        "figure": "Figura",
        "appendix": "Anexo",
        "contents": "Índice",
        "page": "Página",
        "toc_hint": "Actualiza el índice en Word (F9) para ver los números de página.",
        "source": "Fuente",
    },
    "en": {
        "table": "Table",
        "figure": "Figure",
        "appendix": "Appendix",
        "contents": "Contents",
        "page": "Page",
        "toc_hint": "Update the table of contents in Word (F9) to see page numbers.",
        "source": "Source",
    },
}


def labels(language: str | None) -> dict[str, str]:
    return LABELS["en" if str(language or "es").lower().startswith("en") else "es"]


def appendix_letter(index: int) -> str:
    return chr(ord("A") + index) if index < 26 else str(index + 1)


__all__ = ["BLOCK_TYPES", "PAGE_SIZES", "appendix_letter", "labels", "plain", "runs", "validate"]
