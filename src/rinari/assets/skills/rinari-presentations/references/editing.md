# Editing an existing deck

Every edit produces a new revision; the original stays as it was. Address objects by identity, never by order.

## Find the target

1. `documents.inspect` → slide map, unsupported objects and risks.
2. `documents.read` with a selection (`"3"`, `"2-4"`) → each shape with `shape_id`, name, box and text; tables and chart data included.
3. Use `slide` (position from 1) or `slide_id`, plus `shape_id` (or a unique `shape_name`; decks built by Rinari name shapes `rinari:<slide>:<role>`).

## Operations

| Operation | Fields | Notes |
|---|---|---|
| `pptx.set_text` | slide, shape, `text`, `expected_text?` | Keeps the formatting of each paragraph's first run; `\n` starts a paragraph |
| `pptx.replace_text` | `find`, `replace`, `slides?`, `expected_count?`, `include_notes?` | Works across runs; keeps the formatting around the match |
| `pptx.set_table_cell` | slide, shape, `row`, `col` (0-based), `text`, `expected_text?` | |
| `pptx.update_chart` | slide, shape, `categories`, `series[{name, values}]`, `number_format?`, `expected_categories?` | Category charts only; keeps series styling and the number format |
| `pptx.replace_image` | slide, shape, `image` (resource name), `fit?` cover\|contain | Same box; cover crops instead of distorting |
| `pptx.set_notes` | slide, `text`, `expected_text?` | |
| `pptx.delete_slide` | slide, `expected_title?` | Its charts and media go with it; a deck keeps one slide |
| `pptx.move_slide` | slide, `to` | Slide numbers are fields and follow |
| `pptx.add_slide` | `after` (0 = first), `spec` (a DeckSpec slide), `theme?` | Composed on the deck's blank layout |

All operations of one call apply together or not at all.

## Preconditions and conflicts

- Pass `expected_text` (or `expected_count`, `expected_title`, `expected_categories`) with the value you read. A mismatch returns `REVISION_CONFLICT`: read again, never retry blindly.
- `expected_sha256` pins the revision you inspected.

## Preservation

| Policy | Behavior |
|---|---|
| `preserve_strict` (default) | Fails with `PRESERVATION_RISK` if any part changes that the operations did not declare |
| `preserve_best_effort` | Allows undeclared ordinary changes, reports them; still fails on masters, layouts, themes, SmartArt, embeddings or macros |
| `rebuild` | Only when the user asked for a redesign |

The report of the new revision has `changes`, `semantic_diff` (by slide and shape) and the preservation evidence; `documents.diff` compares any two revisions.

## Not supported here

SmartArt, OLE objects, media and animations are preserved untouched but not edited. XY/bubble chart data, `.pptm` macros and legacy `.ppt` are not editable; say so instead of rebuilding the slide.
