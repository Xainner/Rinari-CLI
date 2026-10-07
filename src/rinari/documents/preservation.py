"""Preservación entre revisiones: qué partes cambiaron y si debían cambiar.

Un paquete OOXML se compara parte a parte. El XML se canoniza (C14N) para que
un guardado que solo reordena atributos o declaraciones no cuente como cambio;
el binario se compara por hash. Cada parte se clasifica (diapositiva, layout,
master, gráfico, incrustado, imagen, notas, relaciones…) para que el informe
diga «cambió el gráfico de la diapositiva 4», no «cambió un ZIP».

Una edición declara qué partes toca. Todo lo demás que cambie es riesgo de
preservación: en `preserve_strict` bloquea la revisión.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.documents.contracts import PRESERVATION_POLICIES as POLICIES
from rinari.documents.contracts import PRESERVE_BEST_EFFORT, PRESERVE_STRICT, REBUILD

# Partes que cualquier guardado puede tocar sin alterar el contenido.
_HOUSEKEEPING = ("[Content_Types].xml", "docProps/core.xml", "docProps/app.xml")
MAX_PART_BYTES = 64 * 1024 * 1024

_CATEGORIES = (
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), "slide"),
    (re.compile(r"^ppt/slides/_rels/"), "slide_rels"),
    (re.compile(r"^ppt/notesSlides/"), "notes"),
    (re.compile(r"^ppt/slideLayouts/"), "layout"),
    (re.compile(r"^ppt/slideMasters/"), "master"),
    (re.compile(r"^ppt/notesMasters/"), "notes_master"),
    (re.compile(r"^ppt/theme/|^xl/theme/|^word/theme/"), "theme"),
    (re.compile(r"^(ppt|xl|word)/charts/"), "chart"),
    (re.compile(r"^(ppt|xl|word)/embeddings/"), "embedding"),
    (re.compile(r"^(ppt|xl|word)/media/"), "media"),
    (re.compile(r"^(ppt|xl|word)/diagrams/"), "smartart"),
    (re.compile(r"^ppt/presentation\.xml$"), "presentation"),
    (re.compile(r"^xl/worksheets/"), "worksheet"),
    (re.compile(r"^xl/workbook\.xml$"), "workbook"),
    (re.compile(r"^xl/sharedStrings\.xml$"), "shared_strings"),
    (re.compile(r"^xl/styles\.xml$|^word/styles\.xml$"), "styles"),
    (re.compile(r"^word/document\.xml$"), "body"),
    (re.compile(r"^word/(header|footer)\d*\.xml$"), "header_footer"),
    (re.compile(r"^word/comments"), "comments"),
    (re.compile(r"^xl/drawings/"), "drawing"),
    (re.compile(r"^xl/(pivotTables|pivotCache)/"), "pivot"),
    (re.compile(r"^xl/tables/"), "table"),
    (re.compile(r"^xl/externalLinks/"), "external_link"),
    (re.compile(r"^xl/slicers?/|^xl/slicerCaches/"), "slicer"),
    (re.compile(r"^xl/calcChain\.xml$"), "calc_chain"),
    (re.compile(r"vbaProject\.bin$"), "macros"),
    (re.compile(r"\.rels$"), "relationships"),
    (re.compile(r"^docProps/"), "properties"),
)


# Cambios que nunca son «compatibles»: identidad del documento y contenido activo.
_RISKY = frozenset({"smartart", "embedding", "macros", "master", "layout", "theme"})
# Y pérdidas de objetos que el usuario vería desaparecer.
_RISKY_LOSS = frozenset(
    {"chart", "media", "drawing", "pivot", "table", "external_link", "slicer", "comments"}
)


def category(part: str) -> str:
    for pattern, name in _CATEGORIES:
        if pattern.search(part):
            return name
    return "other"


@dataclass(frozen=True, slots=True)
class Part:
    name: str
    category: str
    digest: str
    size: int


def _canonical(data: bytes) -> bytes:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        return data
    return etree.tostring(root, method="c14n")


def inventory(path: Path) -> dict[str, Part]:
    """Las partes de un paquete OOXML con su huella canónica."""
    parts: dict[str, Part] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > MAX_PART_BYTES:
                continue
            data = archive.read(info.filename)
            if info.filename.endswith((".xml", ".rels")):
                data = _canonical(data)
            parts[info.filename] = Part(
                info.filename,
                category(info.filename),
                hashlib.sha256(data).hexdigest(),
                info.file_size,
            )
    return parts


def diff_packages(before: Path, after: Path) -> dict[str, Any]:
    a, b = inventory(before), inventory(after)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(n for n in set(a) & set(b) if a[n].digest != b[n].digest)
    unchanged = len(set(a) & set(b)) - len(changed)

    def rows(names: list[str], source: dict[str, Part]) -> list[dict[str, Any]]:
        return [{"part": n, "category": source[n].category} for n in names]

    return {
        "added": rows(added, b),
        "removed": rows(removed, a),
        "changed": rows(changed, b),
        "unchanged": unchanged,
    }


def _expand(allowed: set[str]) -> set[str]:
    """Una parte permitida arrastra sus relaciones y, en un slide, sus notas."""
    out = set(allowed)
    for part in allowed:
        directory, name = posixpath.split(part)
        out.add(posixpath.join(directory, "_rels", f"{name}.rels"))
    return out


def evaluate(
    diff: dict[str, Any], allowed: set[str], policy: str, *, allow_new: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Compara el diff con lo que la edición declaró tocar."""
    permitted = _expand(allowed) | set(_HOUSEKEEPING)
    unexpected = []
    for change in ("changed", "removed"):
        for row in diff[change]:
            if row["part"] not in permitted:
                unexpected.append({**row, "change": change})
    for row in diff["added"]:
        if row["part"] not in permitted and not row["part"].startswith(allow_new):
            unexpected.append({**row, "change": "added"})
    risky = [
        row
        for row in unexpected
        if row["category"] in _RISKY
        or (row["change"] == "removed" and row["category"] in _RISKY_LOSS)
    ]
    if not unexpected or policy == REBUILD:
        status = "passed"
    elif policy == PRESERVE_BEST_EFFORT and not risky:
        status = "partial"
    else:
        status = "failed"
    return {
        "status": status,
        "policy": policy,
        "unexpected": unexpected[:100],
        "risky": risky[:100],
    }


def slide_texts(path: Path) -> list[dict[str, Any]]:
    """Texto por diapositiva y forma, para el diff semántico de una presentación."""
    from pptx import Presentation

    presentation = Presentation(str(path))
    slides = []
    for index, slide in enumerate(presentation.slides, start=1):
        shapes = {}
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                shapes[shape.shape_id] = shape.text_frame.text
            elif getattr(shape, "has_table", False) and shape.has_table:
                shapes[shape.shape_id] = "\n".join(
                    "\t".join(cell.text for cell in row.cells) for row in shape.table.rows
                )
            elif getattr(shape, "has_chart", False) and shape.has_chart:
                plot = shape.chart.plots[0] if shape.chart.plots else None
                if plot is not None:
                    shapes[shape.shape_id] = repr(
                        (
                            list(plot.categories),
                            [(s.name, list(s.values)) for s in plot.series],
                        )
                    )
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
        slides.append(
            {"index": index, "slide_id": slide.slide_id, "shapes": shapes, "notes": notes}
        )
    return slides


def semantic_diff(before: Path, after: Path) -> list[dict[str, Any]]:
    """Cambios de contenido de una presentación, por diapositiva y forma."""
    a = {s["slide_id"]: s for s in slide_texts(before)}
    b = {s["slide_id"]: s for s in slide_texts(after)}
    out: list[dict[str, Any]] = []
    order_a = [s for s in a]
    order_b = [s for s in b]
    for slide_id in order_a:
        if slide_id not in b:
            out.append({"change": "slide_removed", "slide_id": slide_id})
    for position, slide_id in enumerate(order_b, start=1):
        if slide_id not in a:
            out.append({"change": "slide_added", "slide_id": slide_id, "index": position})
            continue
        old, new = a[slide_id], b[slide_id]
        for shape_id in sorted(set(old["shapes"]) | set(new["shapes"])):
            before_text = old["shapes"].get(shape_id)
            after_text = new["shapes"].get(shape_id)
            if before_text != after_text:
                out.append(
                    {
                        "change": "shape_removed"
                        if after_text is None
                        else "shape_added"
                        if before_text is None
                        else "content_changed",
                        "slide_id": slide_id,
                        "index": position,
                        "shape_id": shape_id,
                        "before": (before_text or "")[:300],
                        "after": (after_text or "")[:300],
                    }
                )
        if old["notes"] != new["notes"]:
            out.append({"change": "notes_changed", "slide_id": slide_id, "index": position})
    kept_a = [s for s in order_a if s in b]
    kept_b = [s for s in order_b if s in a]
    if kept_a != kept_b:
        out.append({"change": "slides_reordered", "order": kept_b})
    return out[:300]


__all__ = [
    "POLICIES",
    "PRESERVE_BEST_EFFORT",
    "PRESERVE_STRICT",
    "REBUILD",
    "category",
    "diff_packages",
    "evaluate",
    "inventory",
    "semantic_diff",
]
