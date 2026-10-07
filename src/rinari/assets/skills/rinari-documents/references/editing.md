# Editing a Word document

Blocks are the body's paragraphs and tables in order, numbered from 1 as `documents.read` shows them. Every edit produces a new revision; the original stays.

| Operation | Fields | Notes |
|---|---|---|
| `docx.replace_text` | `find`, `replace`, `expected_count?`, `include_headers?` | Works across runs; keeps bold/italic/links around the match |
| `docx.set_paragraph` | `block`, `text`, `expected_text?` | Keeps the paragraph style and its first run's format |
| `docx.insert_paragraph` | `after` (0 = start), `text`, `style?` | The style must exist in the document |
| `docx.insert_table` | `after`, `columns`, `rows`, `style?` | |
| `docx.delete_block` | `block`, `expected_text?` | |
| `docx.set_cell` | `block` (a table), `row`, `col` (0-based), `text`, `expected_text?` | |
| `docx.add_comment` | `block`, `text`, `author?` | A real Word comment, not colored text |

- Pass `expected_text` with what you read; a mismatch is `REVISION_CONFLICT`.
- `preserve_strict` (default) fails if parts other than the document body (or the comments you add) change.
- Comments, fields, hyperlinks and tracked changes you do not touch are preserved; `documents.diff` shows block-level changes.
- Not available here: accepting/rejecting tracked changes, editing footnotes, content controls or equations. Say so; do not rebuild the document to get around it.
- To change a figure or chart, regenerate it and replace the image in the source spec, or ask the user for the source file.
