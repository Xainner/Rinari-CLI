"""Redacción real de PDF rasterizando solo las páginas afectadas.

Una caja negra encima no es redacción: el texto sigue debajo. Aquí cada
página con algo que redactar se sustituye por una imagen de esa página con
las zonas tapadas, así que su texto, sus capas y sus anotaciones dejan de
existir. Las demás páginas quedan como estaban. El documento se escribe
entero de nuevo (sin guardado incremental) y sin metadatos XMP, esquema,
adjuntos ni formularios heredados.

La comprobación usa otra ruta que la localización: se busca con pdfium y se
verifica con pypdf (texto, flujos de contenido, metadatos y anotaciones).
Si algo se recupera, no hay revisión: el trabajo falla.

Límites declarados: una página escaneada no tiene texto que buscar; sus
zonas se dan como regiones. Las páginas rasterizadas pierden el texto
seleccionable.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

DPI = 200
PAD_PT = 1.5
MAX_TERMS = 200
MAX_REGIONS = 2_000


def _invalid(message: str) -> DocumentError:
    return DocumentError(DocumentErrorCode.INVALID_SPEC, message)


def find(path: Path, terms: list[str]) -> list[dict[str, Any]]:
    """Cajas (en puntos, origen abajo a la izquierda) de cada aparición de cada término."""
    import pypdfium2 as pdfium

    from rinari.artifacts.attachments import _PDFIUM_LOCK

    hits: list[dict[str, Any]] = []
    with _PDFIUM_LOCK, contextlib.closing(pdfium.PdfDocument(str(path))) as document:
        for number in range(len(document)):
            with (
                contextlib.closing(document[number]) as page,
                contextlib.closing(page.get_textpage()) as text,
            ):
                for term in terms:
                    searcher = text.search(term, match_case=False)
                    while True:
                        match = searcher.get_next()
                        if not match:
                            break
                        index, count = match
                        for rect in range(text.count_rects(index, count)):
                            left, bottom, right, top = text.get_rect(rect)
                            hits.append(
                                {
                                    "page": number + 1,
                                    "box": [left, bottom, right, top],
                                    "term": term,
                                }
                            )
    return hits


def _regions(regions: Any, sizes: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """Regiones dadas en puntos con origen arriba a la izquierda, como se ve la página."""
    out = []
    for item in regions or []:
        if not isinstance(item, dict) or not isinstance(item.get("page"), int):
            raise _invalid(
                "each region is {page, box: [x0, y0, x1, y1]} in points from the top-left"
            )
        page = item["page"]
        if not 1 <= page <= len(sizes):
            raise _invalid(f"region page must be 1..{len(sizes)}")
        box = item.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(isinstance(v, (int, float)) for v in box)
        ):
            raise _invalid("box is [x0, y0, x1, y1]")
        _, height = sizes[page - 1]
        x0, y0, x1, y1 = box
        out.append(
            {
                "page": page,
                "box": [min(x0, x1), height - max(y0, y1), max(x0, x1), height - min(y0, y1)],
                "term": None,
            }
        )
    return out


def _raster_page(document, number: int, boxes: list[list[float]]) -> bytes:
    from PIL import ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    page = document[number - 1]
    try:
        width, height = page.get_size()
        scale = DPI / 72
        bitmap = page.render(scale=scale, may_draw_forms=True)
        image = bitmap.to_pil().convert("RGB")
    finally:
        page.close()
    draw = ImageDraw.Draw(image)
    for left, bottom, right, top in boxes:
        draw.rectangle(
            [
                (left - PAD_PT) * scale,
                (height - top - PAD_PT) * scale,
                (right + PAD_PT) * scale,
                (height - bottom + PAD_PT) * scale,
            ],
            fill=(0, 0, 0),
        )
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=(width, height))
    sheet.drawImage(ImageReader(image), 0, 0, width, height)
    sheet.showPage()
    sheet.save()
    return buffer.getvalue()


def redact(source: Path, target: Path, *, terms: Any, regions: Any) -> dict[str, Any]:
    import pypdfium2 as pdfium
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject

    from rinari.artifacts.attachments import _PDFIUM_LOCK

    terms = [str(t) for t in (terms or []) if str(t).strip()]
    if len(terms) > MAX_TERMS or len(regions or []) > MAX_REGIONS:
        raise DocumentError(DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, "Too many terms or regions")
    if not terms and not regions:
        raise _invalid("Give terms to find or regions to cover")
    reader = PdfReader(str(source), strict=False)
    if reader.is_encrypted:
        raise DocumentError(DocumentErrorCode.PASSWORD_REQUIRED, "The PDF is encrypted")
    rotated = [n + 1 for n, page in enumerate(reader.pages) if (page.get("/Rotate") or 0) % 360]
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]
    hits = find(source, terms) if terms else []
    targets = hits + _regions(regions, sizes)
    if not targets:
        raise DocumentError(
            DocumentErrorCode.NOT_FOUND,
            "None of the terms appears in the text layer",
            action="If the pages are scanned, give the regions to cover",
        )
    affected = sorted({t["page"] for t in targets})
    blocked = sorted(set(affected) & set(rotated))
    if blocked:
        raise DocumentError(
            DocumentErrorCode.UNSUPPORTED_FEATURE, f"Rotated pages are not redacted yet: {blocked}"
        )
    boxes: dict[int, list[list[float]]] = {}
    for item in targets:
        boxes.setdefault(item["page"], []).append(item["box"])
    rasters: dict[int, bytes] = {}
    with _PDFIUM_LOCK, contextlib.closing(pdfium.PdfDocument(str(source))) as document:
        for number in affected:
            rasters[number] = _raster_page(document, number, boxes[number])

    writer = PdfWriter()
    removed = {"annotations": 0}
    folded = [t.casefold() for t in terms]
    for number, page in enumerate(reader.pages, start=1):
        if number in rasters:
            writer.add_page(PdfReader(io.BytesIO(rasters[number])).pages[0])
            continue
        added = writer.add_page(page)
        annotations = added.get("/Annots")
        if annotations:
            kept = []
            for annotation in annotations:
                body = annotation.get_object()
                text = " ".join(
                    str(body.get(k, "")) for k in ("/Contents", "/T", "/V", "/TU")
                ).casefold()
                if any(term in text for term in folded) or body.get("/Subtype") == "/Widget":
                    removed["annotations"] += 1
                    continue
                kept.append(annotation)
            if kept:
                added[NameObject("/Annots")] = ArrayObject(kept)
            else:
                del added["/Annots"]
    info = reader.metadata or {}
    title = str(info.get("/Title") or "")
    if title and not any(term in title.casefold() for term in folded):
        writer.add_metadata({"/Title": title})
    removed.update(
        metadata=bool(info) or reader.xmp_metadata is not None,
        outline=bool(reader.outline),
        attachments=bool(reader.attachments),
        forms=bool(reader.get_fields()),
    )
    with Path(target).open("wb") as handle:
        writer.write(handle)
    # Por número de término, no por su texto: el informe no copia lo redactado.
    found = {f"term_{i + 1}": sum(1 for h in hits if h["term"] == t) for i, t in enumerate(terms)}
    residual = verify(target, terms, affected if not terms else [])
    if residual:
        raise DocumentError(
            DocumentErrorCode.VALIDATION_FAILED,
            "Redacted content can still be recovered; no revision was produced",
            details={
                "residual": [
                    {**r, "term": f"term_{terms.index(r['term']) + 1}" if r["term"] else None}
                    for r in residual[:50]
                ]
            },
        )
    return {
        "pages_rasterized": affected,
        "terms": found,
        "regions": len(targets) - len(hits),
        "removed": removed,
        "backend": "raster-pdfium",
        "residual": [],
    }


def verify(path: Path, terms: list[str], blank_pages: list[int]) -> list[dict[str, Any]]:
    """Intenta recuperar lo redactado por otra ruta (pypdf), en todo el archivo."""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    residual: list[dict[str, Any]] = []
    folded = [t.casefold() for t in terms]
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").casefold()
        raw = b""
        with contextlib.suppress(Exception):
            raw = page.get_contents().get_data() if page.get_contents() is not None else b""
        lowered = raw.lower()
        for term, original in zip(folded, terms, strict=False):
            if term in text:
                residual.append({"where": "text", "page": number, "term": original})
            for encoding in ("latin-1", "utf-16-be"):
                with contextlib.suppress(UnicodeEncodeError):
                    if original.lower().encode(encoding) in lowered:
                        residual.append(
                            {"where": "content_stream", "page": number, "term": original}
                        )
        for annotation in page.get("/Annots") or []:
            body = annotation.get_object()
            content = " ".join(str(v) for v in body.values()).casefold()
            for term, original in zip(folded, terms, strict=False):
                if term in content:
                    residual.append({"where": "annotation", "page": number, "term": original})
        if number in blank_pages and text.strip():
            residual.append({"where": "text", "page": number, "term": None})
    metadata = " ".join(str(v) for v in (reader.metadata or {}).values()).casefold()
    xmp = reader.xmp_metadata
    for term, original in zip(folded, terms, strict=False):
        if term in metadata:
            residual.append({"where": "metadata", "term": original})
    if xmp is not None:
        residual.append({"where": "xmp", "term": None})
    if reader.attachments:
        residual.append({"where": "attachments", "term": None})
    return residual


__all__ = ["find", "redact", "verify"]
