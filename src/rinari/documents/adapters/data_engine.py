"""Motor de datos para trabajo pesado: DuckDB fuera del contexto del modelo.

Un dataset es una base DuckDB con una tabla `data`, importada una vez desde
un CSV, TSV, Parquet, JSON o XLSX autorizado. Las consultas:

- solo aceptan una sentencia SELECT (lo comprueba el parser de DuckDB);
- adjuntan los datasets en modo lectura;
- se ejecutan con el acceso externo desactivado y la configuración
  bloqueada: ni archivos, ni URLs, ni extensiones, ni volver a activarlo.

Todo corre en el proceso hijo del trabajo documental.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode

SOURCE_KINDS = ("csv", "tsv", "parquet", "json", "xlsx")
MAX_SAMPLE_ROWS = 2_000
DEFAULT_LIMIT = 200
FETCH = 10_000
TABLE = "data"
_ALIAS = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*(\(\d+(,\s*\d+)?\))?$")
# Celdas que una hoja interpretaría como fórmula al abrir un CSV.
_RISKY_CSV = ("=", "+", "-", "@", "\t", "\r")


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _connect(path: Path | str):
    import duckdb

    return duckdb.connect(str(path))


# -- importación ------------------------------------------------------------------------
def _xlsx_to_csv(path: Path, sheet: str | None, target: Path) -> None:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        ws = workbook[sheet] if sheet else workbook.worksheets[0]
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for row in ws.iter_rows(values_only=True):
                writer.writerow(["" if v is None else _plain(v) for v in row])
    finally:
        workbook.close()


def _plain(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def import_source(path: Path, kind: str, options: dict[str, Any], out_db: Path) -> dict[str, Any]:
    """Crea `out_db` con la tabla `data` y devuelve su esquema y perfil."""
    if kind not in SOURCE_KINDS:
        raise DocumentError(
            DocumentErrorCode.UNSUPPORTED_FORMAT, f"Datasets import {', '.join(SOURCE_KINDS)}"
        )
    con = _connect(out_db)
    try:
        source = path
        if kind == "xlsx":
            source = out_db.with_suffix(".csv")
            _xlsx_to_csv(path, options.get("sheet"), source)
            kind = "csv"
        if kind in ("csv", "tsv"):
            args = [_quote(str(source)), "header = true", "sample_size = -1"]
            delimiter = options.get("delimiter") or ("\t" if kind == "tsv" else None)
            if delimiter:
                args.append(f"delim = {_quote(delimiter)}")
            types = options.get("types") or {}
            if types:
                if not isinstance(types, dict):
                    raise DocumentError(DocumentErrorCode.INVALID_SPEC, "types is {column: TYPE}")
                pairs = []
                for column, sql_type in types.items():
                    sql_type = str(sql_type).upper()
                    if not _TYPE.match(sql_type):
                        raise DocumentError(DocumentErrorCode.INVALID_SPEC, f"Bad type {sql_type}")
                    pairs.append(f"{_quote(column)}: {_quote(sql_type)}")
                args.append("types = {" + ", ".join(pairs) + "}")
            reader = f"read_csv({', '.join(args)})"
        elif kind == "parquet":
            reader = f"read_parquet({_quote(str(source))})"
        else:
            reader = f"read_json_auto({_quote(str(source))})"
        try:
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM {reader}")
        except Exception as exc:
            raise DocumentError(
                DocumentErrorCode.VALIDATION_FAILED,
                f"The data could not be imported: {str(exc)[:400]}",
                action="Declare column types (types) or fix the source",
            ) from exc
        profile = describe(con, TABLE)
    finally:
        con.close()
    if kind == "csv" and source != path:
        source.unlink(missing_ok=True)
    return profile


def describe(con, table: str, sample: int = 20) -> dict[str, Any]:
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    columns = [
        {"name": name, "type": str(sql_type)}
        for name, sql_type, *_ in con.execute(f"DESCRIBE {table}").fetchall()
    ]
    if rows:
        stats = {row[0]: row for row in con.execute(f"SUMMARIZE {table}").fetchall()}
        for column in columns:
            entry = stats.get(column["name"])
            if entry is not None:
                # SUMMARIZE: name, type, min, max, approx_unique, avg, std, q25..q75,
                # count, null_percentage
                column.update(
                    min=_json(entry[2]),
                    max=_json(entry[3]),
                    approx_unique=_json(entry[4]),
                    null_percentage=_json(entry[-1]),
                )
    cursor = con.execute(f"SELECT * FROM {table} LIMIT {int(sample)}")
    return {
        "table": table,
        "row_count": rows,
        "columns": columns,
        "sample": [[_json(v) for v in row] for row in cursor.fetchall()],
    }


# -- consultas ----------------------------------------------------------------------------
def check_sql(sql: Any) -> str:
    """Una sola sentencia SELECT; lo demás no se ejecuta."""
    import duckdb

    if not isinstance(sql, str) or not sql.strip():
        raise DocumentError(DocumentErrorCode.INVALID_SPEC, "sql is a SELECT statement")
    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise DocumentError(DocumentErrorCode.INVALID_SPEC, f"SQL error: {str(exc)[:300]}") from exc
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise DocumentError(
            DocumentErrorCode.INVALID_SPEC,
            "Only one SELECT statement is allowed (no COPY, ATTACH, INSERT, PRAGMA…)",
        )
    return sql.strip().rstrip(";")


def locked(datasets: dict[str, str], database: Path | str = ":memory:"):
    """Conexión con los datasets adjuntos de solo lectura y sin acceso externo."""
    con = _connect(database)
    try:
        for alias, path in datasets.items():
            if not _ALIAS.match(alias):
                raise DocumentError(DocumentErrorCode.INVALID_SPEC, f"Bad dataset alias {alias!r}")
            con.execute(f"ATTACH {_quote(str(path))} AS {alias} (READ_ONLY)")
        if len(datasets) == 1:
            con.execute(f"USE {next(iter(datasets))}")
        con.execute("SET enable_external_access = false")
        con.execute("SET autoinstall_known_extensions = false")
        con.execute("SET autoload_known_extensions = false")
        con.execute("SET lock_configuration = true")
    except Exception:
        con.close()
        raise
    return con


def _run(con, sql: str):
    try:
        return con.execute(sql)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(
            DocumentErrorCode.INVALID_SPEC, f"Query failed: {str(exc)[:400]}"
        ) from exc


def rows(datasets: dict[str, str], sql: str) -> tuple[list[dict[str, str]], Iterator[list[Any]]]:
    """Columnas y filas (en bloques) de una consulta, para escribir un libro."""
    sql = check_sql(sql)
    con = locked(datasets)
    cursor = _run(con, sql)
    columns = [{"name": d[0], "type": str(d[1])} for d in cursor.description]

    def generate() -> Iterator[list[Any]]:
        try:
            while True:
                batch = cursor.fetchmany(FETCH)
                if not batch:
                    return
                for row in batch:
                    yield list(row)
        finally:
            con.close()

    return columns, generate()


def query(
    datasets: dict[str, str],
    sql: str,
    *,
    limit: int = DEFAULT_LIMIT,
    into: str | None = None,
    out_dir: Path,
    csv_safe: bool = True,
) -> dict[str, Any]:
    sql = check_sql(sql)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_SAMPLE_ROWS))
    if into == "dataset":
        target = out_dir / f"derived-{uuid.uuid4().hex[:8]}.duckdb"
        con = locked(datasets, target)
        catalog = target.stem
        try:
            _run(con, f"CREATE TABLE {_ident(catalog)}.main.{TABLE} AS {sql}")
            con.execute(f"USE {_ident(catalog)}")
            profile = describe(con, TABLE, sample=min(limit, 50))
        finally:
            con.close()
        return {"into": "dataset", "path": target.name, **profile}
    con = locked(datasets)
    try:
        cursor = _run(con, sql)
        columns = [{"name": d[0], "type": str(d[1])} for d in cursor.description]
        if into == "csv":
            target = out_dir / "result.csv"
            count = 0
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow([c["name"] for c in columns])
                while True:
                    batch = cursor.fetchmany(FETCH)
                    if not batch:
                        break
                    for row in batch:
                        writer.writerow([_csv_cell(v, csv_safe) for v in row])
                    count += len(batch)
            return {"into": "csv", "path": target.name, "columns": columns, "row_count": count}
        batch = cursor.fetchmany(limit + 1)
    finally:
        con.close()
    return {
        "columns": columns,
        "rows": [[_json(v) for v in row] for row in batch[:limit]],
        "truncated": len(batch) > limit,
        "limit": limit,
    }


def _csv_cell(value: Any, safe: bool) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if safe and isinstance(value, str) and value.startswith(_RISKY_CSV):
        # Política contra inyección de fórmulas al abrir el CSV en una hoja.
        return "'" + value
    return value


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, decimal.Decimal):
        return float(value) if abs(value) < 10**15 else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()[:200]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value][:100]
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in list(value.items())[:100]}
    return str(value)


def column_type(sql_type: str) -> str:
    """Tipo de columna de un libro para un tipo de DuckDB."""
    upper = sql_type.upper()
    if upper.startswith(("DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC")):
        return "number"
    if upper in ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER"):
        return "integer"
    if upper == "DATE":
        return "date"
    if upper.startswith("TIMESTAMP"):
        return "datetime"
    if upper == "BOOLEAN":
        return "bool"
    return "text"


__all__ = [
    "SOURCE_KINDS",
    "check_sql",
    "column_type",
    "describe",
    "import_source",
    "locked",
    "query",
    "rows",
]
