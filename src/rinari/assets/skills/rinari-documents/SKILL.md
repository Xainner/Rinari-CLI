---
name: rinari-documents
description: Write, edit and verify professional Word documents (reports, proposals, letters, long documents) with real styles, tables, captions and a table of contents.
version: 1.0.0
risk: medium
can_delegate: true
required_tools:
  - documents.capabilities
  - documents.templates
  - documents.inspect
  - documents.read
  - documents.create
  - documents.edit
  - documents.render
  - documents.validate
  - documents.review
  - documents.finalize
  - skills.read
optional_tools:
  - documents.diff
  - documents.job.get
triggers:
  - word
  - docx
  - informe
  - propuesta
  - carta
  - memoria
  - documento
  - report
---
# Procedure
1. Decide: new document from a ReportSpec, new document from the user's Word template, or an edit of an existing document. Read `references/reports.md` before creating and `references/editing.md` before editing (`skills.read`).
2. Existing document: `documents.inspect` (headings, tables, comments, fields, tracked changes) and `documents.read` only the blocks you need; blocks are numbered and edits address them.
3. New report: `documents.create` with `kind: docx` and a ReportSpec (headings, paragraphs, lists, tables with captions and sources, figures, callouts, KPIs, appendices). The same spec with `kind: pdf` gives the reading PDF; never maintain two versions by hand.
4. Template: `documents.create` with `template` (the user's .docx with `{{ variables }}`) and `context`. Missing variables are errors, not blanks.
5. Edits: `documents.edit` with `docx.*` operations and `expected_text` from what you read. Do not rewrite a whole paragraph to change a word: use `docx.replace_text`, which keeps the formatting around the change.
6. Render and look at every page (`documents.render` with `show`): orphan headings, split tables, empty pages, broken numbering, headers and footers. Record it with `documents.review`, then `documents.finalize`.

# Verification
- `fields` passes only when Word updated the table of contents; otherwise say page numbers update when the user presses F9 in Word.
- Edits: `documents.diff` shows only the intended blocks; comments and untouched parts stay.
- Figures and numbers come from the user's sources; captions and sources are present where data is shown.

# Failure handling
- No Word or LibreOffice: no visual review; report the static checks and deliver an accepted draft only if the user agrees.
- Tracked changes, footnotes, content controls or equations in the source: edit around them; never delete them to simplify.
- `REVISION_CONFLICT`: read the block again; never apply to an approximate match.

# Success criteria
- A styled, editable DOCX (and PDF when asked) from one source, with checks reported as they are and the original untouched.
