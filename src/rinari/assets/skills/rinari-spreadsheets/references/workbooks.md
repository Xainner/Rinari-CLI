# Workbooks: create and edit

## WorkbookSpec (documents.create, kind xlsx)

```json
{
  "title": "Presupuesto 2027",
  "language": "es",
  "theme": "executive-light",
  "sheets": [
    {"name": "Supuestos", "title": "Supuestos",
     "cells": [{"cell": "A4", "value": "Crecimiento", "style": "label"},
               {"cell": "B4", "value": 0.08, "format": "0.0%", "style": "input", "name": "Crecimiento"}]},
    {"name": "Ventas", "columns": [
        {"header": "Región", "type": "text"},
        {"header": "Código", "type": "id"},
        {"header": "Ventas", "type": "currency", "currency": "EUR", "total": "sum"},
        {"header": "Plan", "type": "currency", "formula": "[@Ventas]*(1+Crecimiento)", "total": "sum"}],
     "data": {"dataset": "ventas", "sql": "select region, codigo, sum(ventas) from data group by 1, 2"},
     "table": {"name": "Ventas"}},
    {"name": "Resumen", "cells": [{"cell": "B3", "formula": "=SUM(Ventas[Plan])", "style": "output"}],
     "charts": [{"type": "bar", "title": "Plan por región", "categories": "A5:A9",
                 "series": [{"name": "Plan", "values": "B5:B9"}], "anchor": "D3"}]}
  ]
}
```

- Unknown fields are rejected; `documents.templates` (kind `xlsx`) lists every field.
- Column types: text, id (always text), number, integer, currency, percent, date (ISO input), datetime, bool.
- Cell styles: `input` (yellow, editable assumptions), `output` (bold results), `label`, `heading`, `note`.
- Data never becomes a formula: only `formula` fields are formulas. A table `formula` column uses structured references (`[@Ventas]`).
- Defined names (`name` on a cell, or `names` at workbook level) make formulas readable.
- Charts are native and reference ranges on the same sheet; bar charts read top-down in data order.

## Good model structure

Separate sheets for sources/data, assumptions (inputs), calculations and results; a short guide or notes sheet when someone else will use it. Do not hard-code a number in a formula that belongs in an input cell. Do not wrap everything in IFERROR to hide errors.

## Editing an existing workbook (documents.edit)

| Operation | Path | Policy |
|---|---|---|
| `xlsx.set_cells` `{sheet, cells[{cell, value|formula, expected?}]}` | OOXML patch of that sheet only | strict |
| `xlsx.add_sheet`, `xlsx.set_format`, `xlsx.set_column_width`, `xlsx.add_validation`, `xlsx.add_conditional_format`, `xlsx.freeze` | openpyxl rewrite | best effort or rebuild |

- `expected` is the current value (or `=formula`) you read; a mismatch is `REVISION_CONFLICT`.
- Not supported: renaming a table header, writing inside a merged range (write its first cell), array formulas, the anchor of a shared formula, inserting rows that shift references. Say so instead of working around it.
- Changing a formula removes cached results for the workbook (Excel recalculates on open); run `spreadsheets.recalculate` to get fresh results.
- `preserve_best_effort` rewrites the package: the report lists what changed, and losing charts, pivots, tables or external links fails the edit.
