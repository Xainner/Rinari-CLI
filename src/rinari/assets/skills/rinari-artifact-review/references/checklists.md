# Review checklists

## Content

- The title of each slide/section states its message, and the body supports it.
- Figures, units, periods and sources agree between text, tables and charts.
- Nothing claims more than the data shows; no invented sources or numbers.
- Required texts (acceptance criteria) are present: `documents.validate` with `expected_text`.
- Language and number separators are consistent with the document's language.

## Design (from the renders)

- Text is never cut, hidden or overlapping other text unintentionally.
- Body text is legible (14 pt or more on slides, 10 pt minimum for notes and sources).
- Contrast is sufficient, including colored badges and chart labels.
- Alignment, margins and spacing are consistent; slides are neither empty nor crowded.
- Chart: axis starts are honest, labels readable, legend needed only for several series.
- Tables: numbers right-aligned, consistent decimals, highlighted row meaningful.
- Visual hierarchy is clear and the same theme is kept throughout.

## Edits and preservation

- `documents.diff` against the parent: only the intended slides/shapes changed.
- Unsupported objects (SmartArt, OLE, media, animations) are still present.
- Notes, hidden slides and charts' embedded data survived.

## Scope

- Pages reviewed vs pages in the document.
- Renderer used (Office, LibreOffice, pdfium) and that it is the revision being delivered.
