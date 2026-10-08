# ReportSpec: one source for DOCX and PDF

```json
{
  "title": "Informe comercial 2026",
  "subtitle": "Resultados y recomendaciones",
  "author": "Dirección comercial",
  "date": "2026-11-15",
  "language": "es",
  "theme": "executive-light",
  "page": {"size": "A4", "orientation": "portrait", "margins_cm": 2.5},
  "toc": true,
  "header": "Informe comercial 2026",
  "footer": "Confidencial",
  "blocks": [
    {"type": "heading", "text": "1. Resumen ejecutivo", "level": 1},
    {"type": "paragraph", "text": "Las ventas crecieron un **12 %**; el margen se mantuvo *estable*."},
    {"type": "kpis", "items": [{"label": "Ventas", "value": "16,8 M", "delta": "+14 %"}]},
    {"type": "bullets", "items": ["Ampliar Norte", "Blindar Sur"], "numbered": false},
    {"type": "table", "caption": "Ventas por región", "columns": ["Región", "Ventas (M)"],
     "rows": [["Norte", 6.1], ["Sur", 2.6]], "source": "ERP", "widths": [3, 1]},
    {"type": "image", "image": "grafico", "width_cm": 14, "caption": "Evolución mensual"},
    {"type": "callout", "title": "Decisión", "text": "Aprobar antes del 15 de diciembre.", "tone": "warning"},
    {"type": "quote", "text": "…", "author": "…"},
    {"type": "page_break"},
    {"type": "appendix", "title": "Metodología"}
  ]
}
```

- Unknown fields are rejected; `documents.templates` (kind `docx` or `pdf`) lists them.
- Inline text supports only `**bold**` and `*italic*`.
- Images are passed in `resources` (`{"grafico": "artifact://…" or a project path}`).
- Numbers in tables follow `language` (es: 1.284,5) and are right-aligned; decimals are uniform per column.

## What the outputs contain

| | DOCX | PDF |
|---|---|---|
| Styles | Title, Heading 1–3, List Bullet/Number, Caption, Quote | Same hierarchy, embedded TrueType fonts |
| Tables | Header row repeated on each page, rows not split | Header repeated, zebra rows |
| Captions | "Tabla N" / "Figura N" as SEQ fields | Numbered text |
| Page numbers | PAGE field in the footer | "Página N" |
| Table of contents | TOC field; Word updates it when installed (`fields: passed`), otherwise the user presses F9 | Real page numbers (two passes) |
| Outline | Headings | PDF bookmarks |

## Writing well

- One idea per paragraph; headings say what the section concludes, not just its topic.
- Tables: only the rows that support the point; long detail goes to an appendix.
- Every table or figure with data has a source; every number traces to the user's material.
- Avoid fake structure: no manual numbering with spaces, no empty paragraphs for spacing, no text in images.

## Word templates

`documents.create` with `template` (a .docx with `{{ variable }}`, `{% for item in items %}…{% endfor %}`) and `context`. The template runs in a sandbox: no attribute tricks, imports or files; a variable missing from `context` stops the build; values are escaped, so `<`, `&` appear as typed.
