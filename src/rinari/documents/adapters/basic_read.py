"""Inspección y lectura acotadas de PDF, DOCX y XLSX.

Inventario y contenido localizable para ver y planificar; la edición de cada
formato vive en su adaptador. Todo lo que no se lee entero lo dice
(`truncated`, cursor), nada se recorta en silencio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

READ_CHAR_BUDGET = 24_000
SHEET_MAP = 200
CELL_SCAN = 200_000


def _selection(selection: str | None, count: int) -> list[int]:
    from rinari.documents.adapters.pptx_read import parse_selection

    return parse_selection(selection, count) if count else []


# -- PDF -------------------------------------------------------------------------


def inspect_pdf(path: Path) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    pages = len(reader.pages)
    fields = reader.get_fields() or {}
    metadata = {k.lstrip("/"): str(v)[:200] for k, v in (reader.metadata or {}).items()}
    sizes = set()
    for page in reader.pages[:50]:
        box = page.mediabox
        sizes.add((round(float(box.width)), round(float(box.height))))
    return {
        "kind": "pdf",
        "page_count": pages,
        "page_sizes_pt": [{"width": w, "height": h} for w, h in sorted(sizes)],
        "form_fields": [
            {"name": name, "type": str(field.get("/FT", "")).lstrip("/")}
            for name, field in list(fields.items())[:200]
        ],
        "metadata": metadata,
        "outline": bool(reader.outline),
    }


def read_pdf(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    import contextlib

    import pypdfium2 as pdfium

    from rinari.artifacts.attachments import _PDFIUM_LOCK

    out: list[dict[str, Any]] = []
    used = 0
    next_cursor = None
    with _PDFIUM_LOCK, contextlib.closing(pdfium.PdfDocument(str(path))) as document:
        wanted = _selection(selection, len(document))
        if cursor:
            wanted = [n for n in wanted if n >= cursor]
        for number in wanted:
            with (
                contextlib.closing(document[number - 1]) as page,
                contextlib.closing(page.get_textpage()) as textpage,
            ):
                text = (textpage.get_text_range() or "").strip()
            if out and used + len(text) > READ_CHAR_BUDGET:
                next_cursor = number
                break
            # Sin capa de texto no es «vacía»: puede ser un escaneo.
            out.append({"page": number, "text": text[:READ_CHAR_BUDGET], "has_text": bool(text)})
            used += len(text)
    return {
        "kind": "pdf",
        "pages": out,
        "truncated": next_cursor is not None,
        "next_cursor": next_cursor,
    }


# -- DOCX ------------------------------------------------------------------------


def _docx(path: Path):
    from docx import Document

    try:
        return Document(str(path))
    except Exception as exc:
        raise DocumentError(
            DocumentErrorCode.UNSUPPORTED_FORMAT, f"The document cannot be opened: {exc}"
        ) from exc


def _blocks(document):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def inspect_docx(path: Path) -> dict[str, Any]:
    from docx.table import Table

    document = _docx(path)
    paragraphs = tables = images = 0
    headings: list[dict[str, Any]] = []
    styles: dict[str, int] = {}
    for index, block in enumerate(_blocks(document)):
        if isinstance(block, Table):
            tables += 1
            continue
        paragraphs += 1
        style = block.style.name if block.style is not None else "Normal"
        styles[style] = styles.get(style, 0) + 1
        if (
            style.lower().startswith(("heading", "título", "titulo", "title"))
            and len(headings) < 200
        ):
            headings.append({"block": index + 1, "style": style, "text": block.text[:200]})
        images += len(block._element.findall(".//{*}drawing"))
    body = document.element.body
    xml = body.xml
    return {
        "kind": "docx",
        "paragraphs": paragraphs,
        "tables": tables,
        "images": images,
        "sections": len(document.sections),
        "headings": headings,
        "styles": dict(sorted(styles.items(), key=lambda kv: -kv[1])[:30]),
        "comments": len(getattr(document, "comments", []) or []),
        "fields": xml.count("w:fldChar") // 2 + xml.count("<w:fldSimple"),
        "tracked_changes": xml.count("<w:ins ") + xml.count("<w:del "),
        "footnotes": xml.count("w:footnoteReference"),
    }


def read_docx(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    from docx.table import Table

    document = _docx(path)
    blocks = list(_blocks(document))
    wanted = _selection(selection, len(blocks))
    if cursor:
        wanted = [n for n in wanted if n >= cursor]
    out: list[dict[str, Any]] = []
    used = 0
    next_cursor = None
    for number in wanted:
        block = blocks[number - 1]
        if isinstance(block, Table):
            rows = [[cell.text[:300] for cell in row.cells][:30] for row in block.rows][:100]
            item: dict[str, Any] = {"block": number, "type": "table", "rows": rows}
            size = sum(len(c) for row in rows for c in row)
        else:
            item = {
                "block": number,
                "type": "paragraph",
                "style": block.style.name if block.style is not None else None,
                "text": block.text[:4000],
                "runs": len(block.runs),
            }
            size = len(item["text"])
        if out and used + size > READ_CHAR_BUDGET:
            next_cursor = number
            break
        out.append(item)
        used += size
    return {
        "kind": "docx",
        "blocks": out,
        "block_count": len(blocks),
        "truncated": next_cursor is not None,
        "next_cursor": next_cursor,
    }


# -- XLSX ------------------------------------------------------------------------


def inspect_xlsx(path: Path) -> dict[str, Any]:
    """Mapa del libro sin recorrer rectángulos vacíos: dimensión declarada,
    celdas materializadas (hasta un tope) y objetos por hoja."""
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=False, keep_links=False)
    sheets: list[dict[str, Any]] = []
    scanned = 0
    try:
        for sheet in workbook.worksheets[:SHEET_MAP]:
            dimension = (
                sheet.calculate_dimension() if hasattr(sheet, "calculate_dimension") else None
            )
            row: dict[str, Any] = {
                "name": sheet.title,
                "state": sheet.sheet_state,
                "declared_range": dimension,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
            }
            values = formulas = 0
            complete = True
            for cells in sheet.iter_rows(values_only=True):
                for value in cells:
                    scanned += 1
                    if value is None:
                        continue
                    values += 1
                    if isinstance(value, str) and value.startswith("="):
                        formulas += 1
                if scanned > CELL_SCAN:
                    complete = False
                    break
            row.update(values=values, formulas=formulas, counted_fully=complete)
            sheets.append(row)
            if scanned > CELL_SCAN:
                break
        names = (
            list(workbook.defined_names.keys())[:200] if hasattr(workbook, "defined_names") else []
        )
    finally:
        workbook.close()
    return {
        "kind": "xlsx",
        "sheet_count": len(sheets),
        "sheets": sheets,
        "defined_names": names,
        "cells_scanned": scanned,
        "truncated": scanned > CELL_SCAN,
    }


def read_xlsx(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    """Un rango concreto (`Hoja!A1:F40`, por defecto el principio de la primera hoja).

    Una celda con fórmula trae la fórmula y, aparte, su resultado en caché
    (`cached`), que puede faltar o estar desfasado: no se confunden.
    """
    import warnings

    import openpyxl
    from openpyxl.utils import range_boundaries

    warnings.filterwarnings("ignore", module="openpyxl")
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=False, keep_links=False)
    values = openpyxl.load_workbook(str(path), read_only=True, data_only=True, keep_links=False)
    try:
        sheet_name, _, ref = (selection or "").rpartition("!")
        sheet_name = sheet_name.strip("'")
        if sheet_name and sheet_name not in workbook.sheetnames:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"No sheet named {sheet_name!r}")
        sheet = workbook[sheet_name] if sheet_name else workbook.worksheets[0]
        cached_sheet = values[sheet.title]
        if not ref:
            ref = "A1:Z60"
        try:
            min_col, min_row, max_col, max_row = range_boundaries(ref)
        except (ValueError, TypeError) as exc:
            raise DocumentError(DocumentErrorCode.INVALID_SPEC, f"Bad range {ref!r}") from exc
        if (max_row - min_row + 1) * (max_col - min_col + 1) > 20_000:
            raise DocumentError(
                DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, "Read at most 20000 cells"
            )
        if cursor:
            min_row = max(min_row, cursor)
        rows = []
        used = 0
        next_cursor = None
        bounds = {"min_row": min_row, "max_row": max_row, "min_col": min_col, "max_col": max_col}
        pairs = zip(sheet.iter_rows(**bounds), cached_sheet.iter_rows(**bounds), strict=False)
        for index, (cells, cached_cells) in enumerate(pairs, start=min_row):
            row = []
            for cell, cached in zip(cells, cached_cells, strict=False):
                if cell.value is None:
                    continue
                entry: dict[str, Any] = {"cell": cell.coordinate, "value": _cell_value(cell.value)}
                if getattr(cell, "data_type", None) == "f":
                    entry["formula"] = True
                    entry["cached"] = _cell_value(cached.value)
                if getattr(cell, "number_format", "General") not in ("General", None):
                    entry["format"] = cell.number_format
                row.append(entry)
            size = sum(len(str(c["value"])) + len(str(c.get("cached", ""))) for c in row)
            if rows and used + size > READ_CHAR_BUDGET:
                next_cursor = index
                break
            rows.append({"row": index, "cells": row})
            used += size
        dimension = sheet.calculate_dimension() if hasattr(sheet, "calculate_dimension") else None
        return {
            "kind": "xlsx",
            "sheet": sheet.title,
            "sheets": workbook.sheetnames,
            "dimension": dimension,
            "range": ref,
            "rows": rows,
            "truncated": next_cursor is not None,
            "next_cursor": next_cursor,
        }
    finally:
        workbook.close()
        values.close()


def _cell_value(value: Any) -> Any:
    import datetime as dt

    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float) and value != value:
        return None
    return value
