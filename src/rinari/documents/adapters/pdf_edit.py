"""Manipulación de PDF con pypdf: páginas, formularios y metadatos.

No es edición semántica: no se reescriben párrafos de un PDF. Para cambiar el
contenido se edita la fuente (DOCX o spec) y se regenera.

Formularios AcroForm: los valores se escriben en el árbol de campos y se
generan sus apariencias, para que se vean en cualquier visor. Aplanar es una
opción explícita (el formulario deja de ser editable), nunca automática. La
verificación lee el valor de los campos y, por una ruta independiente (una
copia aplanada), comprueba que el texto se ve en la página.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

OPERATIONS = (
    "pdf.merge",
    "pdf.select_pages",
    "pdf.delete_pages",
    "pdf.reorder",
    "pdf.rotate",
    "pdf.fill_form",
    "pdf.set_metadata",
)
_FIELDS: dict[str, set[str]] = {
    "pdf.merge": {"documents", "position"},
    "pdf.select_pages": {"pages"},
    "pdf.delete_pages": {"pages", "expected_count"},
    "pdf.reorder": {"order"},
    "pdf.rotate": {"pages", "degrees"},
    "pdf.fill_form": {"fields", "flatten"},
    "pdf.set_metadata": {"title", "author", "subject", "keywords"},
}
MAX_PAGES = 5_000


@dataclass(slots=True)
class EditState:
    changes: list[dict[str, Any]] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    flattened: bool = False


def _invalid(message: str, **details: Any) -> DocumentError:
    return DocumentError(DocumentErrorCode.INVALID_SPEC, message, details=details)


def validate(operations: Any) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or not operations:
        raise _invalid("operations must be a non-empty list")
    for index, op in enumerate(operations, start=1):
        if not isinstance(op, dict) or op.get("op") not in _FIELDS:
            raise _invalid(f"operation {index}: unknown op", ops=list(OPERATIONS))
        unknown = sorted(set(op) - {"op"} - _FIELDS[op["op"]])
        if unknown:
            raise _invalid(
                f"operation {index} ({op['op']}): unknown fields {', '.join(unknown)}",
                allowed=sorted(_FIELDS[op["op"]]),
            )
    return operations


def _pages(value: Any, count: int) -> list[int]:
    from rinari.documents.adapters.pptx_read import parse_selection

    if isinstance(value, list):
        pages = [int(v) for v in value]
    elif isinstance(value, str):
        pages = parse_selection(value, count)
    else:
        raise _invalid("pages is a list or a selection like '1-3,7'")
    bad = [p for p in pages if not 1 <= p <= count]
    if bad:
        raise _invalid(f"pages out of range 1..{count}: {bad[:10]}")
    return pages


def _reader(path: Path):
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise DocumentError(
            DocumentErrorCode.PASSWORD_REQUIRED, "The PDF is encrypted; it is not modified"
        )
    return reader


def form_fields(reader) -> dict[str, dict[str, Any]]:
    fields = reader.get_fields() or {}
    out: dict[str, dict[str, Any]] = {}
    for name, item in fields.items():
        kind = str(item.get("/FT", "")).lstrip("/")
        options = item.get("/Opt") or item.get("/_States_") or []
        flags = int(item.get("/Ff", 0) or 0)
        out[name] = {
            "type": {"Tx": "text", "Btn": "button", "Ch": "choice", "Sig": "signature"}.get(
                kind, kind
            ),
            "value": None if item.get("/V") is None else str(item.get("/V")),
            "options": [str(o if not isinstance(o, list) else o[-1]) for o in options][:100],
            "read_only": bool(flags & 1),
            "required": bool(flags & 2),
        }
    return out


def apply(
    source: Path, target: Path, operations: list[dict[str, Any]], inputs: dict[str, str]
) -> EditState:
    from pypdf import PdfWriter

    validate(operations)
    state = EditState()
    reader = _reader(source)
    writer = PdfWriter(clone_from=reader)
    for index, op in enumerate(operations, start=1):
        kind = op["op"]
        count = len(writer.pages)
        try:
            if kind == "pdf.merge":
                documents = op.get("documents")
                if not isinstance(documents, list) or not documents:
                    raise _invalid("documents is a list of revisions or artifact:// PDFs")
                position = op.get("position", count)
                if not isinstance(position, int) or not 0 <= position <= count:
                    raise _invalid(f"position must be 0..{count}")
                added = 0
                for ref in documents:
                    path = inputs.get(str(ref))
                    if path is None:
                        raise DocumentError(DocumentErrorCode.NOT_FOUND, f"{ref} was not provided")
                    other = _reader(Path(path))
                    writer.merge(position + added, other)
                    added += len(other.pages)
                if len(writer.pages) > MAX_PAGES:
                    raise DocumentError(
                        DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, f"At most {MAX_PAGES} pages"
                    )
                state.changes.append({"op": kind, "pages_added": added, "at": position})
            elif kind == "pdf.select_pages":
                keep = _pages(op.get("pages"), count)
                for number in sorted(set(range(1, count + 1)) - set(keep), reverse=True):
                    writer.remove_page(number - 1)
                state.changes.append({"op": kind, "kept": len(keep)})
            elif kind == "pdf.delete_pages":
                drop = sorted(set(_pages(op.get("pages"), count)), reverse=True)
                if op.get("expected_count") is not None and int(op["expected_count"]) != count:
                    raise DocumentError(
                        DocumentErrorCode.REVISION_CONFLICT,
                        f"The PDF has {count} pages, expected {op['expected_count']}",
                    )
                if len(drop) >= count:
                    raise _invalid("A PDF keeps at least one page")
                for number in drop:
                    writer.remove_page(number - 1)
                state.changes.append({"op": kind, "removed": len(drop)})
            elif kind == "pdf.reorder":
                order = op.get("order")
                if not isinstance(order, list) or sorted(order) != list(range(1, count + 1)):
                    raise _invalid(f"order must be a permutation of 1..{count}")
                pages = [writer.pages[n - 1] for n in order]
                fresh = PdfWriter()
                for page in pages:
                    fresh.add_page(page)
                if writer._root_object.get("/AcroForm") is not None:
                    raise DocumentError(
                        DocumentErrorCode.UNSUPPORTED_FEATURE,
                        "Reordering a form PDF is not supported",
                    )
                writer = fresh
                state.changes.append({"op": kind, "order": order[:50]})
            elif kind == "pdf.rotate":
                degrees = op.get("degrees")
                if degrees not in (90, 180, 270, -90):
                    raise _invalid("degrees is 90, 180, 270 or -90")
                for number in _pages(op.get("pages"), count):
                    writer.pages[number - 1].rotate(int(degrees))
                state.changes.append({"op": kind, "degrees": degrees})
            elif kind == "pdf.fill_form":
                values = op.get("fields")
                if not isinstance(values, dict) or not values:
                    raise _invalid("fields is {name: value}")
                known = form_fields(reader)
                missing = sorted(set(values) - set(known))
                if missing:
                    raise DocumentError(
                        DocumentErrorCode.NOT_FOUND,
                        f"Unknown form fields: {', '.join(missing[:20])}",
                        details={"fields": sorted(known)[:200]},
                    )
                for name in values:
                    info = known[name]
                    if info["read_only"]:
                        raise _invalid(f"Field {name!r} is read-only")
                    if info["type"] == "signature":
                        raise DocumentError(
                            DocumentErrorCode.UNSUPPORTED_FEATURE, "Signature fields are not filled"
                        )
                    if (
                        info["type"] == "choice"
                        and info["options"]
                        and str(values[name]) not in info["options"]
                    ):
                        raise _invalid(f"{name!r} accepts {info['options'][:20]}")
                text = {k: str(v) for k, v in values.items()}
                flatten = bool(op.get("flatten"))
                for page in writer.pages:
                    writer.update_page_form_field_values(
                        page, text, auto_regenerate=False, flatten=flatten
                    )
                writer.set_need_appearances_writer(not flatten)
                state.filled.update(text)
                state.flattened = state.flattened or flatten
                state.changes.append({"op": kind, "fields": sorted(text), "flatten": flatten})
            elif kind == "pdf.set_metadata":
                metadata = {
                    f"/{k.capitalize()}": str(v)[:500]
                    for k, v in op.items()
                    if k != "op" and v is not None
                }
                writer.add_metadata(metadata)
                state.changes.append({"op": kind, "fields": sorted(metadata)})
        except DocumentError as exc:
            exc.details = {**(exc.details or {}), "operation": index, "op": kind}
            raise
    with Path(target).open("wb") as handle:
        writer.write(handle)
    return state


def verify_form(path: Path, expected: dict[str, str]) -> dict[str, Any]:
    """Valores en el árbol de campos y, por otra ruta, en la apariencia que se dibuja.

    La segunda comprobación no usa el valor del campo: lee el flujo de
    apariencia (/AP /N) de cada widget, que es lo que un visor pinta.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    fields = form_fields(reader)
    stored = {name: fields.get(name, {}).get("value") for name in expected}
    wrong = {n: v for n, v in stored.items() if (v or "") != expected[n]}
    drawn: dict[str, bool] = {name: False for name in expected}
    for page in reader.pages:
        for annotation in page.get("/Annots") or []:
            widget = annotation.get_object()
            name = widget.get("/T")
            parent = widget.get("/Parent")
            if name is None and parent is not None:
                name = parent.get_object().get("/T")
            if name is None or str(name) not in expected:
                continue
            appearance = (widget.get("/AP") or {}).get("/N")
            if appearance is None:
                continue
            stream = appearance.get_object()
            try:
                data = stream.get_data()
            except Exception:
                continue
            value = expected[str(name)]
            if _shows(data, value):
                drawn[str(name)] = True
    invisible = [name for name, ok in drawn.items() if expected[name] and not ok]
    return {"stored": not wrong, "visible": not invisible, "wrong": wrong, "invisible": invisible}


def _shows(data: bytes, value: str) -> bool:
    """¿El flujo de apariencia escribe este texto? (literal o hexadecimal)."""
    if not value:
        return True
    for encoding in ("latin-1", "utf-16-be", "utf-8"):
        try:
            raw = value.encode(encoding)
        except UnicodeEncodeError:
            continue
        escaped = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        if raw in data or escaped in data or raw.hex().encode() in data.lower():
            return True
    return False


def page_texts(path: Path, limit: int = 2_000) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    return [(page.extract_text() or "")[:limit] for page in reader.pages[:MAX_PAGES]]


__all__ = [
    "OPERATIONS",
    "EditState",
    "apply",
    "form_fields",
    "page_texts",
    "validate",
    "verify_form",
]
