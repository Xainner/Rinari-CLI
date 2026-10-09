# Large data with spreadsheets.query

## Datasets

- `source`: a project path or `artifact://` (CSV, TSV, Parquet, JSON, XLSX). It is imported once into a DuckDB dataset (table `data`); the same file returns the same dataset.
- Options for the import: `types` (`{"codigo": "VARCHAR"}` keeps identifiers with leading zeros as text), `delimiter`, `sheet` (XLSX), `format` when the name has no extension, `source_name` for the alias.
- Without `sql` the tool describes the dataset: columns and types, row count, min/max, approx. distinct values, null percentage and a 20-row sample. Start there.

## Queries

- One `SELECT` per call. Single dataset: `FROM data`. Several: pass `datasets` and use `alias.data` (alias = dataset name in lowercase).
- The engine runs with external access off: no files, URLs, `COPY`, `ATTACH`, extensions or settings. That is by design.
- `limit` returns up to 2000 rows (default 200) and says `truncated`. Prefer aggregates: `GROUP BY`, `count(*)`, `sum`, `avg`, `quantile_cont`, window functions.
- `into: "dataset"` keeps a large result as a new dataset (with its parent and query) for later steps or for a workbook sheet.
- `into: "csv"` writes the full result as a CSV artifact; text starting with `= + - @` is prefixed with `'` so a spreadsheet does not run it as a formula.

## Checks before trusting a number

- Row counts before and after joins; duplicates on keys (`count(*) - count(DISTINCT key)`).
- Nulls in the columns you aggregate; how they were treated.
- Dates: type DATE vs text; time zone of timestamps; fiscal vs calendar periods.
- Units and currency of each amount column; percentages as fractions (0.08) vs points (8).
- Identifiers as text, never as numbers.

## From data to workbook

A sheet with `data: {"dataset": "totales", "sql": "select * from data order by total desc"}` is filled by the engine (columns and types come from the query). Use `profile: "streaming"` for hundreds of thousands of rows; it writes rows sequentially and supports columns, widths and freeze only.
