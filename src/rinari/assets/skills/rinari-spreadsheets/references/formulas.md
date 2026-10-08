# Formulas, caches and recalculation

## States of a formula result

| cache_state | Meaning |
|---|---|
| `missing` | Formula written, no result stored (every workbook Rinari creates starts here) |
| `stale` | Results exist but inputs or formulas changed after they were computed |
| `unknown` | A workbook from someone else: results exist, nothing proves they match the inputs |
| `fresh` | Excel recalculated this exact revision |

`documents.read` shows each formula cell with `formula: true` and its `cached` value separately. A cached value is not proof; only `fresh` is.

## Recalculate

`spreadsheets.recalculate` opens the revision in its own, isolated Excel instance (macros disabled, links not updated, events off), runs a full calculation and saves a new revision (`operation: calculate`). The report lists formula errors (#REF!, #NAME?, #DIV/0!, #VALUE!, #N/A, #NUM!, #SPILL!…) with their cells. If Excel has the user's workbooks open, the calculation is refused rather than touching them.

Without Excel installed: formulas stay, results are pending (`formulas: not_run`, `NOT_CALCULATED`). Never type computed values over formulas to "fix" that unless the user asks for static values.

## Writing formulas

- English function names and comma separators (OOXML), e.g. `=SUMIFS(Ventas[Ventas],Ventas[Región],A5)`.
- Prefer structured references and defined names over hard-coded ranges.
- Dynamic-array functions (FILTER, UNIQUE, XLOOKUP, LET, LAMBDA) are written as future functions; check them after recalculation.
- Errors that are intentional (e.g. `#N/A` for a missing lookup) must be documented next to the cell; otherwise an error is a defect.

## Verify

1. `spreadsheets.recalculate` → no error cells.
2. Read the key outputs (`documents.read`) and compare with an independent `spreadsheets.query` over the source data.
3. `documents.validate` → `formulas: passed` on the revision you deliver.
