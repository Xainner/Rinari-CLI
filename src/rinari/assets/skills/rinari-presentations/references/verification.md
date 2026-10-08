# Verifying a deck

## Dimensions

| Check | Source | Passed means |
|---|---|---|
| `structure` | Package opens, every relationship resolves | The file is sound |
| `layout` | Static composition: bounds, minimum sizes, estimated overflow, contrast, unexpected overlaps, chart series | No error findings; warnings are listed |
| `content` | `expected_text` you pass | Every required text is present |
| `preservation` | Edits only: part-by-part diff against the parent | Only declared parts changed |
| `visual` | Your `documents.review` over the renders of this exact revision | Every page was reviewed and no critical/error finding remains |

Statuses are `passed`, `failed`, `partial`, `not_run` or `not_applicable`, each with evidence. Report them as they are.

## Visual review loop

1. `documents.render` the revision (created/edited revisions are rendered automatically when a renderer exists).
2. Look at every page with `show` (6 per call). Check: cut or overlapping text, legibility from a distance, alignment, hierarchy (the title is the message), chart labels and units, empty or crowded slides, consistency across slides.
3. Record it: `documents.review` with `pages` and `findings` (`severity` critical|error|warning|suggestion, `page`, `message`, `fix`, optional `shape_id`).
4. Fix errors with `documents.edit`, which creates a new revision, render it and review again. Stop when the budget is exhausted and deliver the draft with its findings rather than approving it.

The review belongs to the revision whose renders you saw. A new revision needs a new review.

## Finalize

`documents.finalize` delivers only when structure, layout, preservation and visual pass. With `accept_partial` (only if the user accepts it) the revision is labeled `accepted_draft` and keeps its pending checks. `save_to` copies it into the project under a free name; it never overwrites a file.
