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

## Not available (say so)

Secure redaction (needs a certified backend; drawing boxes is not redaction), digital signatures, PDF/A or PDF/UA conformance, editing paragraphs inside an existing PDF.
