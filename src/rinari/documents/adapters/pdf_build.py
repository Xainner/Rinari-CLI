"""Compilador de ReportSpec a PDF con ReportLab (platypus).

Composición por componentes (párrafos con estilo, tablas que repiten su
cabecera, figuras, índice con los números de página reales en dos pasadas),
no coordenadas a mano. Las fuentes TrueType se incrustan: el texto es
seleccionable y los caracteres españoles y los signos (menos, —, €) se ven.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.documents.adapters import report_spec
from rinari.documents.contracts import DocumentError, DocumentErrorCode
from rinari.documents.design.tokens import Theme, theme

CM = 28.3465
SHORT_TABLE_ROWS = 30


@dataclass(slots=True)
class BuildPlan:
    pages: int = 0
    headings: int = 0
    tables: int = 0
    figures: int = 0
    toc: bool = False
    fonts: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)


_REGISTERED: dict[str, str] = {}


def _font_files(family: str) -> dict[str, Path | None]:
    from rinari.documents.design import measure

    regular = measure.font_file(family, False)
    bold = measure.font_file(family, True)
    italic = None
    if regular is not None:
        stem = regular.stem.lower()
        for candidate in (f"{stem}i", f"{stem}-italic", stem.replace("regular", "italic")):
            path = regular.with_name(candidate + regular.suffix)
            if path.is_file():
                italic = path
                break
    return {"regular": regular, "bold": bold, "italic": italic}


def register_family(family: str) -> str:
    """Registra la familia TTF y devuelve su nombre en ReportLab (o la base 14 si no hay)."""
    if family in _REGISTERED:
        return _REGISTERED[family]
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [family, "Calibri", "Segoe UI", "Arial", "DejaVu Sans", "Vera"]
    for name in candidates:
        files = _font_files(name) if name != "Vera" else {}
        if name == "Vera":
            # ReportLab trae Vera: siempre hay una TrueType que incrustar.
            import reportlab

            base = Path(reportlab.__file__).parent / "fonts"
            files = {
                "regular": base / "Vera.ttf",
                "bold": base / "VeraBd.ttf",
                "italic": base / "VeraIt.ttf",
            }
        regular = files.get("regular")
        if regular is None or not Path(regular).is_file():
            continue
        key = f"R-{name.replace(' ', '')}"
        pdfmetrics.registerFont(TTFont(key, str(regular)))
        bold = files.get("bold") if files.get("bold") and Path(files["bold"]).is_file() else regular
        italic = (
            files.get("italic")
            if files.get("italic") and Path(files["italic"]).is_file()
            else regular
        )
        pdfmetrics.registerFont(TTFont(f"{key}-B", str(bold)))
        pdfmetrics.registerFont(TTFont(f"{key}-I", str(italic)))
        pdfmetrics.registerFont(TTFont(f"{key}-BI", str(bold)))
        addMapping(key, 0, 0, key)
        addMapping(key, 1, 0, f"{key}-B")
        addMapping(key, 0, 1, f"{key}-I")
        addMapping(key, 1, 1, f"{key}-BI")
        _REGISTERED[family] = key
        return key
    raise DocumentError(DocumentErrorCode.FONT_UNAVAILABLE, f"No TrueType font for {family}")


def _hex(value: str):
    from reportlab.lib import colors

    return colors.HexColor("#" + value.lstrip("#"))


def _markup(text: str) -> str:
    from xml.sax.saxutils import escape

    out = []
    for piece, bold, italic in report_spec.runs(text):
        piece = escape(piece)
        if bold:
            piece = f"<b>{piece}</b>"
        if italic:
            piece = f"<i>{piece}</i>"
        out.append(piece)
    return "".join(out)


class _Builder:
    def __init__(self, spec: dict[str, Any], resources: dict[str, str]) -> None:
        self.spec = report_spec.validate(spec)
        self.theme: Theme = theme(spec.get("theme"))
        self.labels = report_spec.labels(spec.get("language"))
        self.language = str(spec.get("language") or "es")
        self.resources = resources
        self.plan = BuildPlan()
        t = self.theme
        self.body = register_family(t.body_font)
        self.heading = register_family(t.heading_font)
        self.plan.fonts = sorted({self.body, self.heading})
        self.ink = "1F2937"
        self.accent = t.accent if not t.dark else "2F5BEA"
        self._styles()

    def _styles(self) -> None:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.styles import ParagraphStyle

        ink, accent = _hex(self.ink), _hex(self.accent)
        muted = _hex("5A6372")
        self.styles = {
            "body": ParagraphStyle(
                "body",
                fontName=self.body,
                fontSize=10.5,
                leading=15,
                textColor=ink,
                spaceAfter=6,
                alignment=TA_LEFT,
            ),
            "toc_title": ParagraphStyle(
                "toc_title",
                fontName=f"{self.heading}-B",
                fontSize=18,
                leading=22,
                textColor=accent,
                spaceAfter=10,
            ),
            "h1": ParagraphStyle(
                "h1",
                fontName=f"{self.heading}-B",
                fontSize=18,
                leading=22,
                textColor=accent,
                spaceBefore=16,
                spaceAfter=8,
                keepWithNext=1,
            ),
            "h2": ParagraphStyle(
                "h2",
                fontName=f"{self.heading}-B",
                fontSize=14,
                leading=18,
                textColor=ink,
                spaceBefore=12,
                spaceAfter=6,
                keepWithNext=1,
            ),
            "h3": ParagraphStyle(
                "h3",
                fontName=f"{self.heading}-B",
                fontSize=12,
                leading=15,
                textColor=ink,
                spaceBefore=10,
                spaceAfter=4,
                keepWithNext=1,
            ),
            "title": ParagraphStyle(
                "title",
                fontName=self.heading,
                fontSize=30,
                leading=36,
                textColor=_hex("111827"),
                spaceAfter=14,
            ),
            "subtitle": ParagraphStyle(
                "subtitle",
                fontName=self.body,
                fontSize=14,
                leading=19,
                textColor=muted,
                spaceAfter=10,
            ),
            "meta": ParagraphStyle(
                "meta", fontName=self.body, fontSize=10, leading=14, textColor=muted, spaceBefore=18
            ),
            "caption": ParagraphStyle(
                "caption",
                fontName=self.body,
                fontSize=8.5,
                leading=11,
                textColor=muted,
                spaceBefore=3,
                spaceAfter=10,
            ),
            "cell": ParagraphStyle(
                "cell", fontName=self.body, fontSize=9, leading=11.5, textColor=ink
            ),
            "cell_head": ParagraphStyle(
                "cell_head", fontName=self.body, fontSize=9, leading=11.5, textColor=_hex("FFFFFF")
            ),
            "quote": ParagraphStyle(
                "quote",
                fontName=self.heading,
                fontSize=13,
                leading=18,
                textColor=ink,
                leftIndent=18,
                spaceBefore=8,
                spaceAfter=4,
            ),
            "bullet": ParagraphStyle(
                "bullet",
                fontName=self.body,
                fontSize=10.5,
                leading=15,
                textColor=ink,
                leftIndent=16,
                bulletIndent=4,
                spaceAfter=3,
            ),
            "toc1": ParagraphStyle("toc1", fontName=self.body, fontSize=11, leading=17),
            "toc2": ParagraphStyle(
                "toc2", fontName=self.body, fontSize=10, leading=15, leftIndent=14
            ),
            "toc3": ParagraphStyle(
                "toc3", fontName=self.body, fontSize=9.5, leading=14, leftIndent=28
            ),
            "kpi_label": ParagraphStyle(
                "kpi_label", fontName=self.body, fontSize=8.5, leading=11, textColor=muted
            ),
            "kpi_value": ParagraphStyle(
                "kpi_value", fontName=self.body, fontSize=17, leading=21, textColor=ink
            ),
        }

    # -- flujo -----------------------------------------------------------------------
    def _p(self, text: str, style: str):
        from reportlab.platypus import Paragraph

        return Paragraph(_markup(text), self.styles[style])

    def _raw(self, markup: str, style: str):
        """Un párrafo cuyo marcado ya está escapado y compuesto aquí."""
        from reportlab.platypus import Paragraph

        return Paragraph(markup, self.styles[style])

    def _table(self, block: dict[str, Any], width: float) -> list[Any]:
        from reportlab.platypus import Paragraph, Table, TableStyle

        from rinari.documents.adapters.pptx_build import _decimals, _format, _is_number

        columns = [str(c) for c in block["columns"]]
        rows = block["rows"]
        numeric = [
            bool(rows)
            and all(_is_number(r[c]) for r in rows if c < len(r) and r[c] not in (None, ""))
            for c in range(len(columns))
        ]
        decimals = [
            max(
                (_decimals(r[c]) for r in rows if c < len(r) and isinstance(r[c], float)), default=0
            )
            for c in range(len(columns))
        ]
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle

        right = ParagraphStyle("cell_r", parent=self.styles["cell"], alignment=TA_RIGHT)
        right_head = ParagraphStyle("cell_hr", parent=self.styles["cell_head"], alignment=TA_RIGHT)
        data = [
            [
                Paragraph(
                    f"<b>{_markup(h)}</b>", right_head if numeric[i] else self.styles["cell_head"]
                )
                for i, h in enumerate(columns)
            ]
        ]
        for values in rows:
            line = []
            for col in range(len(columns)):
                value = values[col] if col < len(values) else None
                text = _format(value, self.language, decimals[col]) if value is not None else ""
                line.append(
                    Paragraph(_markup(str(text)), right if numeric[col] else self.styles["cell"])
                )
            data.append(line)
        widths = block.get("widths")
        if isinstance(widths, list) and len(widths) == len(columns):
            total = sum(float(w) for w in widths) or 1
            col_widths = [width * float(w) / total for w in widths]
        else:
            col_widths = None
        table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="CENTER")
        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), _hex(self.accent)),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, _hex("D9DDE3")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for index in range(2, len(data), 2):
            commands.append(("BACKGROUND", (0, index), (-1, index), _hex("F3F4F6")))
        if isinstance(block.get("highlight_row"), int):
            row = block["highlight_row"] + 1
            commands.append(("FONTNAME", (0, row), (-1, row), f"{self.body}-B"))
        table.setStyle(TableStyle(commands))
        out: list[Any] = []
        self.plan.tables += 1
        if block.get("caption"):
            out.append(
                self._p(f"{self.labels['table']} {self.plan.tables}. {block['caption']}", "caption")
            )
        out.append(table)
        if block.get("source"):
            out.append(self._p(f"{self.labels['source']}: {block['source']}", "caption"))
        else:
            from reportlab.platypus import Spacer

            out.append(Spacer(1, 8))
        if len(rows) <= SHORT_TABLE_ROWS:
            # Una tabla corta no se parte dejando una fila sola; va entera con su pie.
            from reportlab.platypus import KeepTogether

            return [KeepTogether(out)]
        return out

    def _image(self, block: dict[str, Any], width: float) -> list[Any]:
        from PIL import Image as PILImage
        from reportlab.platypus import Image

        path = self.resources.get(str(block["image"]))
        if path is None:
            raise DocumentError(
                DocumentErrorCode.NOT_FOUND, f"Image {block['image']!r} not provided"
            )
        with PILImage.open(path) as image:
            iw, ih = image.size
        target = min(width, float(block.get("width_cm") or 99) * CM)
        flowable = Image(path, width=target, height=target * ih / iw)
        flowable.hAlign = "CENTER"
        self.plan.figures += 1
        caption = f"{self.labels['figure']} {self.plan.figures}"
        if block.get("caption"):
            caption += f". {block['caption']}"
        return [flowable, self._p(caption, "caption")]

    def _boxed(self, flowables: list[Any], fill: str, width: float):
        from reportlab.platypus import Table, TableStyle

        box = Table([[flowables]], colWidths=[width])
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _hex(fill)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return box

    def _kpis(self, block: dict[str, Any], width: float):
        from reportlab.platypus import Table, TableStyle

        t = self.theme
        cells = []
        for item in block["items"]:
            parts = [
                self._p(str(item.get("label") or ""), "kpi_label"),
                self._raw(f"<b>{_markup(str(item.get('value', '')))}</b>", "kpi_value"),
            ]
            if item.get("delta"):
                text = str(item["delta"])
                negative = text.strip().startswith(("-", "\u2212"))
                color = (
                    (t.negative if negative else t.positive)
                    if not t.dark
                    else ("C8363D" if negative else "0E7A5F")
                )
                parts.append(
                    self._raw(f'<font color="#{color}"><b>{_markup(text)}</b></font>', "kpi_label")
                )
            cells.append(parts)
        gap = 6
        col = (width - gap * (len(cells) - 1)) / len(cells)
        table = Table([cells], colWidths=[col] * len(cells))
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _hex("F3F4F6")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEAFTER", (0, 0), (-2, -1), gap, _hex("FFFFFF")),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return table

    def build(self) -> bytes:
        from reportlab.lib.pagesizes import A4, LETTER, landscape
        from reportlab.platypus import (
            BaseDocTemplate,
            Frame,
            KeepTogether,
            ListFlowable,
            ListItem,
            NextPageTemplate,
            PageBreak,
            PageTemplate,
            Paragraph,
            Spacer,
        )
        from reportlab.platypus.tableofcontents import TableOfContents

        spec = self.spec
        page = spec.get("page") or {}
        size = LETTER if page.get("size") == "Letter" else A4
        if page.get("orientation") == "landscape":
            size = landscape(size)
        margin = float(page.get("margins_cm", 2.5)) * CM
        width = size[0] - 2 * margin
        plan = self.plan
        builder = self
        buffer = io.BytesIO()
        heading_styles = ("h1", "h2", "h3")

        class Doc(BaseDocTemplate):
            def afterFlowable(self, flowable):
                if isinstance(flowable, Paragraph) and flowable.style.name in heading_styles:
                    level = heading_styles.index(flowable.style.name)
                    text = flowable.getPlainText()
                    key = f"h-{id(flowable)}"
                    self.canv.bookmarkPage(key)
                    self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
                    self.notify("TOCEntry", (level, text, self.page, key))

        doc = Doc(
            buffer,
            pagesize=size,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
            title=str(spec.get("title") or ""),
            author=str(spec.get("author") or ""),
            subject=str(spec.get("subtitle") or ""),
            creator="Rinari",
            lang=self.language,
        )
        frame = Frame(margin, margin, width, size[1] - 2 * margin, id="body")
        footer_text = str(spec.get("footer") or "")
        header_text = str(spec.get("header") or "")

        def decorate(canvas, document):
            canvas.saveState()
            canvas.setFont(builder.body, 8.5)
            canvas.setFillColor(_hex("5A6372"))
            if header_text:
                canvas.drawString(margin, size[1] - margin * 0.6, header_text)
            canvas.drawRightString(
                size[0] - margin, margin * 0.5, f"{builder.labels['page']} {document.page}"
            )
            if footer_text:
                canvas.drawString(margin, margin * 0.5, footer_text)
            canvas.restoreState()

        doc.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[frame]),
                PageTemplate(id="body", frames=[frame], onPage=decorate),
            ]
        )
        story: list[Any] = []
        cover = spec.get("cover", bool(spec.get("title"))) and spec.get("title")
        if cover:
            story += [Spacer(1, size[1] * 0.25), self._p(str(spec["title"]), "title")]
            if spec.get("subtitle"):
                story.append(self._p(str(spec["subtitle"]), "subtitle"))
            meta = " · ".join(str(v) for v in (spec.get("author"), spec.get("date")) if v)
            if meta:
                story.append(self._p(meta, "meta"))
            story += [NextPageTemplate("body"), PageBreak()]
        else:
            doc.pageTemplates.reverse()
        if spec.get("toc"):
            toc = TableOfContents()
            toc.levelStyles = [self.styles["toc1"], self.styles["toc2"], self.styles["toc3"]]
            toc.dotsMinLevel = 0
            story += [self._p(self.labels["contents"], "toc_title"), toc, PageBreak()]
            plan.toc = True
        appendices = 0
        for block in spec["blocks"]:
            kind = block["type"]
            if kind == "heading":
                story.append(self._p(str(block["text"]), f"h{block.get('level', 1)}"))
                plan.headings += 1
            elif kind == "paragraph":
                story.append(self._p(str(block["text"]), "body"))
            elif kind == "bullets":
                items = [ListItem(self._p(str(i), "body"), leftIndent=16) for i in block["items"]]
                listing = ListFlowable(
                    items,
                    bulletType="1" if block.get("numbered") else "bullet",
                    start=None if block.get("numbered") else "•",
                    leftIndent=16,
                    bulletFontName=self.body,
                    bulletFontSize=10,
                    bulletColor=_hex(self.accent),
                )
                # Una lista corta no deja su último punto solo en la página siguiente.
                story.append(KeepTogether([listing]) if len(items) <= 8 else listing)
            elif kind == "table":
                story += self._table(block, width)
            elif kind == "image":
                story.append(KeepTogether(self._image(block, width)))
            elif kind == "quote":
                parts = [self._raw(f"<i>{_markup(str(block['text']))}</i>", "quote")]
                if block.get("author"):
                    parts.append(self._p(f"— {block['author']}", "caption"))
                story.append(KeepTogether(parts))
            elif kind == "callout":
                fill = {"info": "EEF3FE", "warning": "FFF6D6", "success": "E8F6EF"}[
                    block.get("tone", "info")
                ]
                inner = []
                if block.get("title"):
                    inner.append(self._raw(f"<b>{_markup(str(block['title']))}</b>", "body"))
                inner.append(self._p(str(block["text"]), "body"))
                story += [self._boxed(inner, fill, width), Spacer(1, 8)]
            elif kind == "kpis":
                story += [self._kpis(block, width), Spacer(1, 10)]
            elif kind == "page_break":
                story.append(PageBreak())
            elif kind == "appendix":
                letter = report_spec.appendix_letter(appendices)
                appendices += 1
                story += [
                    PageBreak(),
                    self._p(f"{self.labels['appendix']} {letter}. {block['title']}", "h1"),
                ]
                plan.headings += 1
        doc.multiBuild(_keep_headings(story))
        data = buffer.getvalue()
        from pypdf import PdfReader

        plan.pages = len(PdfReader(io.BytesIO(data)).pages)
        return data


def _keep_headings(story: list[Any]) -> list[Any]:
    """Un título nunca queda solo al pie de página: viaja con el bloque que lo sigue."""
    from reportlab.platypus import KeepTogether, Paragraph

    out: list[Any] = []
    index = 0
    while index < len(story):
        item = story[index]
        is_heading = isinstance(item, Paragraph) and item.style.name in ("h1", "h2", "h3")
        if is_heading and index + 1 < len(story):
            group = [item]
            index += 1
            # Títulos seguidos (capítulo y sección) y el primer bloque, juntos.
            while index < len(story):
                following = story[index]
                group.append(following)
                index += 1
                if not (
                    isinstance(following, Paragraph) and following.style.name in ("h1", "h2", "h3")
                ):
                    break
            out.append(KeepTogether(group))
            continue
        out.append(item)
        index += 1
    return out


def build(spec: dict[str, Any], resources: dict[str, str] | None = None) -> tuple[bytes, BuildPlan]:
    builder = _Builder(spec, resources or {})
    data = builder.build()
    return data, builder.plan


__all__ = ["BuildPlan", "build", "register_family"]
