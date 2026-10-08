---
name: rinari-pdf
description: Create professional PDFs, manipulate pages, fill forms, redact for real and read PDFs reliably; never claims signatures or conformance it cannot prove.
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
  - pdf.redact
  - documents.diff
  - documents.job.get
triggers:
  - pdf
  - formulario
  - rellenar
  - unir pdf
  - separar páginas
  - redactar
  - anonimizar
  - form
---
# Procedure
1. Identify the job: authoring a new PDF, manipulating an existing one (pages, forms, metadata), or changing its content. Read `references/pdf.md` (`skills.read`).
2. Authoring: `documents.create` with `kind: pdf` and a ReportSpec (the same spec as Word). If a DOCX/PPTX source exists, change the source and regenerate; never patch the PDF text.
3. Existing PDF: `documents.inspect` lists pages, sizes, form fields (type, value, options, read-only) and metadata; `documents.read` gives the text per page and says which pages have no text layer (scans).
4. Manipulation: `documents.edit` with `pdf.*` operations (merge, select/delete/reorder/rotate pages, fill_form, set_metadata).
5. Forms: fill only fields that exist, with allowed options; keep the form editable unless the user asks to flatten.
6. Redaction: `pdf.redact` with the exact terms (and regions for scanned pages). Tell the user first that the affected pages become images without selectable text, and pass `confirm: true` only after that.
7. Render and look at the affected pages (`documents.render` with `show`), record `documents.review`, then `documents.finalize`.

# Verification
- `text_layer`: text is selectable and fonts are embedded; pages without text are listed.
- `forms`: values are stored in the fields and drawn in their appearance; both must pass.
- Page counts and order after manipulation match the request (`documents.diff`).

# Failure handling
- Encrypted PDFs (`PASSWORD_REQUIRED`) are not modified; ask for an unprotected copy.
- A black rectangle drawn over text is not redaction; only `pdf.redact` removes the content, and it fails (no revision) if anything can still be recovered.
- Scanned pages have no text to find: give their regions, and say which pages could only be covered by region.
- A graphic signature is not a digital signature; signing and PDF/A or PDF/UA conformance are not claimed.

# Success criteria
- The PDF does what was asked, keeps selectable text, and its checks are reported with their real state.
