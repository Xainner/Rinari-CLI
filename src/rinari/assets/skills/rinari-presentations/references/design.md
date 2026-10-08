# Designing a deck

The compiler computes geometry; you decide the story and the layout of each slide.

## DeckSpec

```json
{
  "title": "Revisión comercial Q3",
  "language": "es",
  "theme": "executive-light",
  "slides": [
    {"layout": "cover", "title": "...", "subtitle": "...", "eyebrow": "...", "meta": "..."},
    {"layout": "chart", "title": "Norte y Centro concentran el crecimiento",
     "chart": {"type": "column", "categories": ["Norte", "Sur"],
               "series": [{"name": "Crecimiento %", "values": [31, 4]}],
               "number_format": "0\"%\""},
     "takeaway": "...", "source": "Fuente: CRM", "notes": "Speaker notes"}
  ]
}
```

- Unknown fields are rejected. Every slide accepts `id`, `layout`, `notes` and `source`; the other fields per layout are listed by `documents.templates`.
- `language` sets number separators in tables (`es`: 1.284,5; `en`: 1,284.5).
- Images go in `resources` (`{"logo": "artifact://..." or a project path}`) and slides reference the name.
- A very large spec can be saved as a JSON artifact and passed as `spec_uri`.

## Themes

| Theme | Use |
|---|---|
| `executive-light` | Default for business: committees, clients, reports |
| `executive-dark` | On-screen presentations, product reviews |
| `editorial` | Narrative, research, long-form; serif headings |
| `rinari` | Only material about Rinari itself |

## Layouts and their capacity

| Layout | Message | Capacity (calibrated) |
|---|---|---|
| `cover`, `closing` | Opening and close | Title up to ~2 lines |
| `section` | Chapter break | Title + optional number |
| `statement` | One dominant message | One sentence, support line |
| `summary` | Executive summary | 2–6 points, `{head, text}` |
| `bullets` | Simple list | 1–8 points; prefer another layout above 5 |
| `kpi` | 1–4 figures with variation | Short labels, `delta`, `trend` up/down/flat, `good` |
| `chart` | Data with a conclusion | Native editable chart + `takeaway` |
| `comparison` | 2–3 options | 1–6 points per column |
| `matrix` | 2×2 | 4 quadrants, optional axes |
| `table` | Detail | ≤8 columns, ≤14 rows; move more to an appendix |
| `timeline` | Dates | 2–6 events |
| `process` | Steps | 2–6 steps |
| `image` | A picture that carries meaning | Caption optional |
| `quote` | A voice | Quote + author/role |

## Rules that make it look professional

- One idea per slide, stated in the title as a conclusion ("Norte explica dos tercios del crecimiento"), not a topic ("Ventas").
- Vary layouts by function; twenty slides of title + bullets is a failure.
- Numbers come from the user's data. Never invent figures, sources or axes; a truncated axis that exaggerates a difference is wrong.
- Body text stays at 14 pt or more and footnotes at 10 pt; when text does not fit, the build reports `TEXT_OVERFLOW` instead of shrinking it — summarize, split or move to an appendix.
- Charts stay native (editable data in PowerPoint). Do not draw charts with shapes or paste whole slides as images.
- Keep the same theme across the deck; the user's template or brand wins over any theme.

## Wrong vs right

- Wrong: a `bullets` slide with eight long sentences. Right: `summary` with four `{head, text}` points, details in notes.
- Wrong: a table of 30 rows on one slide. Right: the 5 rows that support the message, the rest in an appendix table.
- Wrong: a chart without a takeaway. Right: a chart whose title states what it shows and a `takeaway` that says what to do.
