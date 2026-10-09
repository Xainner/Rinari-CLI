# PDF jobs

## Three different jobs

| Job | How |
|---|---|
| Authoring | `documents.create` `kind: pdf` with a ReportSpec (see rinari-documents `references/reports.md`): ReportLab components, embedded fonts, repeated table headers, bookmarks and a table of contents with real page numbers |
| Manipulation | `documents.edit` with the operations below |
| Content change | Edit the source (DOCX, PPTX, spec) and regenerate; a PDF is not edited like a Word file |

## Operations

| Operation | Fields | Notes |
|---|---|---|
| `pdf.merge` | `documents[]` (PDF revisions or `artifact://`), `position?` | Inserts their pages |
| `pdf.select_pages` | `pages` (`"1-3,7"`) | Keeps only those |
| `pdf.delete_pages` | `pages`, `expected_count?` | A PDF keeps at least one page |
| `pdf.reorder` | `order` (permutation of 1..n) | Not on form PDFs |
| `pdf.rotate` | `pages`, `degrees` 90/180/270 | |
| `pdf.fill_form` | `fields {name: value}`, `flatten?` | Choice fields accept only their options; read-only and signature fields are refused |
| `pdf.set_metadata` | `title?`, `author?`, `subject?`, `keywords?` | |

## Forms

1. `documents.inspect` → `form_fields` (name, type, current value, options).
2. Fill with exact field names. The engine writes the value and its appearance; the `forms` check verifies both (stored in the field tree and drawn in the widget appearance).
3. Flattening (`flatten: true`) makes the form non-editable; do it only on request and say so.

## Reading

`documents.read` returns text per page and `has_text: false` for pages without a text layer (often scans). OCR is a separate step for those pages; never present OCR or table extraction from a PDF as exact without checking.

## Redaction (pdf.redact)

- `terms`: exact texts to remove (case-insensitive), found in the text layer of every page.
- `regions`: `{page, box: [x0, y0, x1, y1]}` in points from the top-left of the page, for scans, signatures, photos or anything that is not text.
- Each affected page is replaced by an image of that page with the areas blacked out; other pages are untouched. The new file is written from scratch without XMP metadata, outline, attachments or forms, and annotations that mention a term are removed.
- An independent check (a different PDF library) searches text, content streams, annotations, metadata and attachments. If anything is recoverable the job fails and no revision is created.
- Costs to tell the user: affected pages lose selectable text; forms and attachments are dropped. Rotated pages are not supported yet.
- The report counts matches per term as `term_1`, `term_2`…; it never stores the redacted text.

## Not available (say so)

Digital signatures, PDF/A or PDF/UA conformance, editing paragraphs inside an existing PDF.
