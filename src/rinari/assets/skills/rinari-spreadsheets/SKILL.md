---
name: rinari-spreadsheets
description: Build, edit, calculate and verify Excel workbooks, and analyze large data (CSV/Parquet/XLSX) with SQL without loading it into context.
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
  - documents.validate
  - documents.finalize
  - spreadsheets.query
  - spreadsheets.recalculate
  - skills.read
optional_tools:
  - documents.render
  - documents.review
  - documents.diff
  - documents.job.get
  - documents.job.cancel
triggers:
  - excel
  - xlsx
  - hoja de cálculo
  - hoja de calculo
  - spreadsheet
  - presupuesto
  - csv
  - parquet
  - tabla dinámica
  - dataset
---
# Procedure
1. Separate the data from the workbook. Large data (thousands of rows or more) stays out of context: `spreadsheets.query` with `source` imports it once as a dataset and, without `sql`, returns schema, row count and a sample. Answer with SQL aggregates, never by reading rows one by one.
2. Read `references/data.md` before querying and `references/workbooks.md` before creating or editing (`skills.read`); `references/formulas.md` when formulas or recalculation are involved.
3. Existing workbook: `documents.inspect` (sheets, declared ranges, formulas, risks such as macros, pivots or external links), then `documents.read` only the ranges you need (`Hoja!A1:H40`); formula cells show the formula and, separately, the cached result.
4. Create with `documents.create` (`kind: xlsx`, a WorkbookSpec): inputs, calculations and results in separate, labeled areas; large tables come from `data: {dataset, sql}` instead of inline rows.
5. Edit with `documents.edit`: `xlsx.set_cells` keeps every other part byte-identical (strict); structure and format operations need `preserve_best_effort` and the report says what changed.
6. Formulas written are not formulas calculated. If results matter, run `spreadsheets.recalculate` (installed Excel) and check the formula errors it reports; without Excel, say the results are pending.
7. Check totals against the dataset with an independent query, then `documents.validate` and `documents.finalize` (`save_to` only when the user wants the file in the project).

# Verification
- Totals and counts in the workbook match a query over the source data; quote both.
- `formulas` is passed only after `spreadsheets.recalculate` on the revision you deliver, with no error cells.
- Never claim a visual or calculated check that the report shows as not run.

# Failure handling
- More rows than one sheet holds (1,048,576): aggregate, split on purpose by period or region, or deliver CSV via `into: csv`; never truncate silently.
- `PRESERVATION_RISK`: use `xlsx.set_cells` or ask before rebuilding a workbook with charts, pivots or macros.
- XLSM/XLSB/XLS are detected, not edited as XLSX; macros never run.
- `CALCULATION_BACKEND_UNAVAILABLE`: keep formulas, mark results pending, deliver as an accepted draft only if the user agrees.

# Success criteria
- The numbers are traceable to the data (dataset, query) and the workbook stays editable with real formulas.
- The original file is untouched and every check is reported with its true state.
