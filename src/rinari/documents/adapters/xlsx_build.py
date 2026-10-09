"""Compilador de WorkbookSpec a XLSX con XlsxWriter.

Dos perfiles, decididos antes de escribir:

- `rich`: tablas con formato, fórmulas, gráficos nativos, validaciones,
  nombres definidos y configuración de impresión.
- `streaming`: exportación por filas (`constant_memory`) de grandes volúmenes;
  sin tablas, gráficos ni celdas sueltas, porque ese modo no las admite.

Los datos nunca se interpretan: un texto que empieza por `=` sigue siendo
texto, igual que una URL o un número escrito como texto. Las fórmulas solo
entran por los campos `formula`, y su resultado queda vacío hasta que un
backend certificado calcule el libro: no se inventa un 0 como caché.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass, field
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode
from rinari.documents.design.tokens import Theme, theme

MAX_SHEETS = 60
MAX_ROWS = 1_048_575  # una fila de Excel queda para la cabecera
MAX_COLUMNS = 16_384
MAX_INLINE_ROWS = 50_000  # filas en el spec; más, desde un dataset
MAX_CHARTS = 20
PROFILES = ("rich", "streaming")
CHART_TYPES = ("column", "bar", "line", "area", "pie", "doughnut", "scatter", "stacked_column")
COLUMN_TYPES = (
    "text",
    "id",
    "number",
    "integer",
    "currency",
    "percent",
    "date",
    "datetime",
    "bool",
)
TOTALS = ("sum", "average", "count", "min", "max")
CELL_STYLES = ("label", "input", "output", "heading", "note")
_SHEET_NAME = re.compile(r"^[^\[\]:*?/\\]{1,31}$")
_CELL = re.compile(r"^\$?([A-Z]{1,3})\$?(\d{1,7})$")
_RANGE = re.compile(r"^\$?[A-Z]{1,3}\$?\d{1,7}(:\$?[A-Z]{1,3}\$?\d{1,7})?$")

WORKBOOK_KEYS = frozenset(
    {"schema_version", "kind", "title", "language", "theme", "profile", "sheets", "names", "author"}
)
SHEET_KEYS = frozenset(
    {
        "name",
        "title",
        "description",
        "columns",
        "rows",
        "data",
        "table",
        "cells",
        "freeze",
        "widths",
        "charts",
        "validations",
        "conditional",
        "print",
        "hidden",
        "notes",
    }
)
STREAMING_SHEET_KEYS = frozenset(
    {"name", "title", "description", "columns", "rows", "data", "freeze", "widths", "hidden"}
)
COLUMN_KEYS = frozenset({"header", "type", "format", "width", "total", "formula", "currency"})
CELL_KEYS = frozenset({"cell", "value", "formula", "format", "style", "name"})
CHART_KEYS = frozenset(
    {"type", "title", "categories", "series", "anchor", "width", "height", "number_format"}
)

_CURRENCY = {
    "EUR": '#,##0.00 "€"',
    "USD": '"$"#,##0.00',
    "GBP": '"£"#,##0.00',
    "MXN": '"$"#,##0.00',
    "COP": '"$"#,##0',
    "CLP": '"$"#,##0',
    "ARS": '"$"#,##0.00',
    "PEN": '"S/" #,##0.00',
    "BRL": '"R$" #,##0.00',
}


@dataclass(slots=True)
class SheetPlan:
    name: str
    table_range: str | None = None
    rows: int = 0
    columns: int = 0
    formulas: int = 0
    charts: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table_range": self.table_range,
            "rows": self.rows,
            "columns": self.columns,
            "formulas": self.formulas,
            "charts": self.charts,
        }


def _invalid(message: str, **details: Any) -> DocumentError:
    return DocumentError(DocumentErrorCode.INVALID_SPEC, message, details=details)


def _closed(value: Any, allowed: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{where} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _invalid(f"{where}: unknown fields {', '.join(unknown)}", allowed=sorted(allowed))
    return value


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def cell_name(row: int, col: int) -> str:
    return f"{column_letter(col)}{row + 1}"


def _absolute(sheet: str, ref: str) -> str:
    if not _RANGE.match(ref.replace("$", "")):
        raise _invalid(f"Bad range {ref!r}")
    parts = [re.sub(r"^\$?([A-Z]+)\$?(\d+)$", r"$\1$\2", p) for p in ref.split(":")]
    quoted = "'" + sheet.replace("'", "''") + "'"
    return f"={quoted}!{':'.join(parts)}"


def validate(spec: Any) -> dict[str, Any]:
    """Esquema cerrado antes de escribir nada."""
    spec = _closed(spec, WORKBOOK_KEYS, "workbook")
    profile = spec.get("profile", "rich")
    if profile not in PROFILES:
        raise _invalid(f"profile must be one of {', '.join(PROFILES)}")
    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise _invalid("sheets must be a non-empty list")
    if len(sheets) > MAX_SHEETS:
        raise DocumentError(
            DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, f"At most {MAX_SHEETS} sheets"
        )
    names: set[str] = set()
    for index, sheet in enumerate(sheets, start=1):
        allowed = STREAMING_SHEET_KEYS if profile == "streaming" else SHEET_KEYS
        _closed(sheet, allowed, f"sheet {index}")
        name = sheet.get("name")
        if not isinstance(name, str) or not _SHEET_NAME.match(name) or name.startswith("'"):
            raise _invalid(f"sheet {index}: name must be 1-31 chars without []:*?/\\")
        if name.casefold() in names:
            raise _invalid(f"Duplicate sheet name {name!r}")
        names.add(name.casefold())
        for column in sheet.get("columns") or []:
            _closed(column, COLUMN_KEYS, f"{name}: column")
            if column.get("type", "text") not in COLUMN_TYPES:
                raise _invalid(f"{name}: column type must be one of {', '.join(COLUMN_TYPES)}")
            if column.get("total") not in (None, *TOTALS):
                raise _invalid(f"{name}: total must be one of {', '.join(TOTALS)}")
        rows = sheet.get("rows")
        if rows is not None and (not isinstance(rows, list) or len(rows) > MAX_INLINE_ROWS):
            raise _invalid(
                f"{name}: rows must be a list of at most {MAX_INLINE_ROWS}; "
                "larger data goes through a dataset (data: {dataset, sql})"
            )
        if rows is not None and sheet.get("data") is not None:
            raise _invalid(f"{name}: use rows or data, not both")
        if sheet.get("data") is not None:
            data = _closed(sheet["data"], frozenset({"dataset", "sql"}), f"{name}: data")
            if not isinstance(data.get("sql"), str) or not data["sql"].strip():
                raise _invalid(f"{name}: data.sql is a SELECT over the dataset")
        for cell in sheet.get("cells") or []:
            _closed(cell, CELL_KEYS, f"{name}: cell")
            if not isinstance(cell.get("cell"), str) or not _CELL.match(cell["cell"]):
                raise _invalid(f"{name}: bad cell address {cell.get('cell')!r}")
            if "value" in cell and "formula" in cell:
                raise _invalid(f"{name}!{cell['cell']}: value or formula, not both")
            if cell.get("style") not in (None, *CELL_STYLES):
                raise _invalid(f"{name}: style must be one of {', '.join(CELL_STYLES)}")
        charts = sheet.get("charts") or []
        if len(charts) > MAX_CHARTS:
            raise _invalid(f"{name}: at most {MAX_CHARTS} charts")
        for chart in charts:
            _closed(chart, CHART_KEYS, f"{name}: chart")
            if chart.get("type") not in CHART_TYPES:
                raise _invalid(f"{name}: chart type must be one of {', '.join(CHART_TYPES)}")
    return spec


class _Builder:
    def __init__(self, spec: dict[str, Any], datasets: dict[str, Any] | None) -> None:
        self.spec = validate(spec)
        self.profile = spec.get("profile", "rich")
        self.theme: Theme = theme(spec.get("theme"))
        self.language = str(spec.get("language") or "es")
        self.datasets = datasets or {}
        self.plans: list[SheetPlan] = []
        self.buffer = io.BytesIO()
        import xlsxwriter

        options = {
            "in_memory": True,
            # Datos ajenos nunca se convierten en fórmulas, URLs ni números.
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
            "use_future_functions": True,
            "default_date_format": self._date_format(),
        }
        if self.profile == "streaming":
            options["constant_memory"] = True
            options["in_memory"] = False
            import tempfile

            options["tmpdir"] = tempfile.gettempdir()
        self.book = xlsxwriter.Workbook(self.buffer, options)
        self._formats: dict[tuple, Any] = {}

    # -- formatos --------------------------------------------------------------------
    def _date_format(self) -> str:
        return "yyyy-mm-dd" if self.language.lower().startswith("en") else "dd/mm/yyyy"

    def fmt(self, **props: Any):
        key = tuple(sorted(props.items()))
        if key not in self._formats:
            base = {"font_name": self.theme.body_font, "font_size": 11, "valign": "top"}
            self._formats[key] = self.book.add_format({**base, **props})
        return self._formats[key]

    def _number_format(self, column: dict[str, Any]) -> str | None:
        if column.get("format"):
            return str(column["format"])
        kind = column.get("type", "text")
        if kind == "number":
            return "#,##0.00"
        if kind == "integer":
            return "#,##0"
        if kind == "percent":
            return "0.0%"
        if kind == "currency":
            code = str(column.get("currency") or "EUR").upper()
            return _CURRENCY.get(code, f'#,##0.00 "{code}"')
        if kind == "date":
            return self._date_format()
        if kind == "datetime":
            return f"{self._date_format()} hh:mm"
        return None

    def _style(self, name: str | None):
        t = self.theme
        if name == "heading":
            return self.fmt(bold=True, font_size=13, font_color=t.text)
        if name == "label":
            return self.fmt(font_color=t.muted)
        if name == "input":
            # Convención de modelos: las entradas se distinguen de los cálculos.
            return self.fmt(
                bg_color="#FFF6D6", font_color="#1F3A93", border=1, border_color=t.border
            )
        if name == "output":
            return self.fmt(bold=True, top=1, top_color=t.border)
        if name == "note":
            return self.fmt(italic=True, font_color=t.muted, text_wrap=True)
        return None

    # -- valores -----------------------------------------------------------------------
    def _write(self, sheet, row: int, col: int, value: Any, column: dict[str, Any], cell_format):
        kind = column.get("type", "text")
        if value is None or value == "":
            sheet.write_blank(row, col, None, cell_format)
            return
        if kind in ("text", "id"):
            sheet.write_string(row, col, str(value), cell_format)
            return
        if kind in ("date", "datetime"):
            parsed = _date(value)
            if parsed is None:
                sheet.write_string(row, col, str(value), cell_format)
                return
            sheet.write_datetime(row, col, parsed, cell_format)
            return
        if kind == "bool" or isinstance(value, bool):
            sheet.write_boolean(row, col, bool(value), cell_format)
            return
        if isinstance(value, (int, float)):
            sheet.write_number(row, col, value, cell_format)
            return
        sheet.write_string(row, col, str(value), cell_format)

    # -- hojas -------------------------------------------------------------------------
    def build(self) -> bytes:
        if self.spec.get("title") or self.spec.get("author"):
            self.book.set_properties(
                {
                    "title": str(self.spec.get("title") or "")[:250],
                    "author": str(self.spec.get("author") or "")[:120],
                    "comments": "Generated by Rinari",
                }
            )
        for spec in self.spec["sheets"]:
            self._sheet(spec)
        for name, ref in (self.spec.get("names") or {}).items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]{0,254}$", str(name)):
                raise _invalid(f"Bad defined name {name!r}")
            self.book.define_name(str(name), "=" + str(ref).lstrip("="))
        self.book.close()
        return self.buffer.getvalue()

    def _sheet(self, spec: dict[str, Any]) -> None:
        t = self.theme
        name = spec["name"]
        sheet = self.book.add_worksheet(name)
        plan = SheetPlan(name=name)
        self.plans.append(plan)
        if spec.get("title"):
            sheet.hide_gridlines(2)
        if spec.get("hidden"):
            sheet.hide()
        row = 0
        if spec.get("title"):
            sheet.write_string(
                row, 0, str(spec["title"]), self.fmt(bold=True, font_size=16, font_color=t.text)
            )
            sheet.set_row(row, 24)
            row += 1
            if spec.get("description"):
                sheet.write_string(row, 0, str(spec["description"]), self.fmt(font_color=t.muted))
                row += 1
            row += 1
        columns = spec.get("columns") or []
        if columns or spec.get("rows") is not None or spec.get("data") is not None:
            row = self._table(sheet, spec, plan, row, columns)
        for cell in spec.get("cells") or []:
            self._cell(sheet, cell, plan)
        for column, width in (spec.get("widths") or {}).items():
            if not re.match(r"^[A-Z]{1,3}$", str(column)) or not isinstance(width, (int, float)):
                raise _invalid(f"{name}: widths are {{'A': 18}}")
            sheet.set_column(f"{column}:{column}", float(width))
        if spec.get("freeze"):
            match = _CELL.match(str(spec["freeze"]))
            if not match:
                raise _invalid(f"{name}: bad freeze cell")
            sheet.freeze_panes(str(spec["freeze"]))
        for chart in spec.get("charts") or []:
            self._chart(sheet, name, chart, plan)
        for validation in spec.get("validations") or []:
            self._validation(sheet, name, validation)
        for rule in spec.get("conditional") or []:
            self._conditional(sheet, name, rule)
        self._print(sheet, spec)
        if spec.get("notes"):
            note_row = max(row + 1, 1)
            sheet.merge_range(
                note_row,
                0,
                note_row + 2,
                max(3, plan.columns - 1),
                str(spec["notes"]),
                self._style("note"),
            )

    def _rows(self, spec: dict[str, Any]):
        if spec.get("data") is not None:
            source = self.datasets.get("__rows__", {}).get(spec["name"])
            if source is None:
                raise DocumentError(
                    DocumentErrorCode.NOT_FOUND, f"{spec['name']}: dataset rows were not provided"
                )
            return source
        return iter(spec.get("rows") or [])

    def _table(self, sheet, spec, plan: SheetPlan, top: int, columns: list[dict[str, Any]]) -> int:
        t = self.theme
        name = spec["name"]
        rows_source = self._rows(spec)
        if not columns:
            first = next(rows_source, None)
            if first is None:
                return top
            columns = [{"header": str(h)} for h in first]
        headers = [str(c.get("header") or f"Columna {i + 1}") for i, c in enumerate(columns)]
        if len(headers) > MAX_COLUMNS:
            raise DocumentError(DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED, "Too many columns")
        formats = []
        for column in columns:
            number = self._number_format(column)
            props: dict[str, Any] = {}
            if number:
                props["num_format"] = number
            if column.get("type") in ("text", "id", None):
                props["text_wrap"] = False
            formats.append(self.fmt(**props))
        header_format = self.fmt(
            bold=True,
            font_color=t.accent_text,
            bg_color=t.accent,
            border=1,
            border_color=t.accent,
            valign="vcenter",
        )
        widths = [max(len(h) + 2, 8) for h in headers]
        use_table = self.profile == "rich" and spec.get("table", True) is not False
        if not use_table:
            for col, header in enumerate(headers):
                sheet.write_string(top, col, header, header_format)
        data_rows: list[list[Any]] | None = [] if use_table else None
        count = 0
        for values in rows_source:
            if not isinstance(values, (list, tuple)):
                raise _invalid(f"{name}: each row is a list of cells")
            if len(values) > len(columns):
                raise _invalid(f"{name}: row {count + 1} has more cells than columns")
            count += 1
            if count > MAX_ROWS - top:
                raise DocumentError(
                    DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED,
                    f"{name}: more than {MAX_ROWS} rows do not fit in one sheet",
                    action="Aggregate first, split by sheet on purpose, or export CSV/Parquet",
                )
            if use_table:
                data_rows.append(list(values))
            else:
                for col, value in enumerate(values):
                    self._write(sheet, top + count, col, value, columns[col], formats[col])
            if count <= 200:
                for col, value in enumerate(values):
                    widths[col] = max(widths[col], min(60, len(_display(value)) + 2))
        plan.rows = count
        plan.columns = len(columns)
        last_row = top + max(count, 1)
        if use_table:
            options = spec.get("table") if isinstance(spec.get("table"), dict) else {}
            totals = any(c.get("total") for c in columns)
            table_columns = []
            for col, column in enumerate(columns):
                entry: dict[str, Any] = {"header": headers[col], "format": formats[col]}
                if column.get("formula"):
                    entry["formula"] = str(column["formula"]).lstrip("=")
                    plan.formulas += max(count, 1)
                if column.get("total"):
                    entry["total_function"] = {"average": "average", "count": "count"}.get(
                        column["total"], column["total"]
                    )
                    plan.formulas += 1
                elif totals and col == 0:
                    entry["total_string"] = "Total"
                table_columns.append(entry)
            table_name = options.get("name") if isinstance(options, dict) else None
            table: dict[str, Any] = {
                "columns": table_columns,
                "style": (options or {}).get("style") or "Table Style Medium 2",
                "autofilter": True,
                "total_row": totals,
            }
            if table_name:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$", str(table_name)):
                    raise _invalid(f"{name}: bad table name")
                table["name"] = str(table_name)
            if data_rows:
                table["data"] = [_cells(r, columns) for r in data_rows]
            end_row = last_row + (1 if totals else 0)
            sheet.add_table(top, 0, end_row, len(columns) - 1, table)
            # Tipos que add_table no respeta (fechas, ids) se escriben encima.
            for offset, values in enumerate(data_rows or [], start=1):
                for col, value in enumerate(values):
                    if columns[col].get("type") in ("date", "datetime", "id"):
                        self._write(sheet, top + offset, col, value, columns[col], formats[col])
            plan.table_range = f"{cell_name(top, 0)}:{cell_name(end_row, len(columns) - 1)}"
        else:
            sheet.autofilter(top, 0, last_row, len(columns) - 1)
            plan.table_range = f"{cell_name(top, 0)}:{cell_name(last_row, len(columns) - 1)}"
        for col, column in enumerate(columns):
            width = column.get("width") or widths[col]
            sheet.set_column(col, col, float(width))
        if not spec.get("freeze"):
            sheet.freeze_panes(top + 1, 0)
        return last_row + 1

    def _cell(self, sheet, cell: dict[str, Any], plan: SheetPlan) -> None:
        address = cell["cell"]
        props: dict[str, Any] = {}
        style = self._style(cell.get("style"))
        if cell.get("format"):
            props["num_format"] = str(cell["format"])
        cell_format = style
        if props:
            base = {
                "heading": {"bold": True, "font_size": 13},
                "label": {"font_color": self.theme.muted},
                "input": {"bg_color": "#FFF6D6", "font_color": "#1F3A93", "border": 1},
                "output": {"bold": True, "top": 1},
                "note": {"italic": True, "font_color": self.theme.muted},
            }.get(cell.get("style") or "", {})
            cell_format = self.fmt(**base, **props)
        if "formula" in cell:
            formula = str(cell["formula"])
            if not formula.startswith("="):
                formula = "=" + formula
            sheet.write_formula(address, formula, cell_format, "")
            plan.formulas += 1
        else:
            value = cell.get("value")
            match = _CELL.match(address)
            row, col = int(match.group(2)) - 1, _column_index(match.group(1))
            self._write(sheet, row, col, value, {"type": _type_of(value)}, cell_format)
        if cell.get("name"):
            quoted = "'" + sheet.name.replace("'", "''") + "'"
            absolute = re.sub(r"^([A-Z]+)(\d+)$", r"$\1$\2", address.replace("$", ""))
            self.book.define_name(str(cell["name"]), f"={quoted}!{absolute}")

    def _chart(self, sheet, sheet_name: str, spec: dict[str, Any], plan: SheetPlan) -> None:
        t = self.theme
        kind = spec["type"]
        options = {"type": kind}
        if kind == "stacked_column":
            options = {"type": "column", "subtype": "stacked"}
        chart = self.book.add_chart(options)
        series = spec.get("series")
        if not isinstance(series, list) or not series:
            raise _invalid(f"{sheet_name}: chart needs series [{{name, values}}]")
        categories = spec.get("categories")
        for index, item in enumerate(series):
            if not isinstance(item, dict) or not isinstance(item.get("values"), str):
                raise _invalid(f"{sheet_name}: series values is a range like B4:B10")
            color = "#" + t.palette[index % len(t.palette)]
            entry: dict[str, Any] = {
                "name": str(item.get("name") or f"Serie {index + 1}"),
                "values": _absolute(sheet_name, item["values"]),
            }
            if isinstance(categories, str):
                entry["categories"] = _absolute(sheet_name, categories)
            if kind in ("line", "scatter"):
                entry["line"] = {"color": color, "width": 2.25}
                entry["marker"] = {
                    "type": "circle",
                    "size": 6,
                    "fill": {"color": color},
                    "border": {"color": color},
                }
            elif kind in ("pie", "doughnut"):
                entry["points"] = [
                    {"fill": {"color": "#" + t.palette[i % len(t.palette)]}} for i in range(12)
                ]
            else:
                entry["fill"] = {"color": color}
                entry["border"] = {"none": True}
                entry["invert_if_negative"] = False
            if spec.get("number_format") and len(series) == 1 and kind not in ("line", "scatter"):
                entry["data_labels"] = {"value": True, "num_format": str(spec["number_format"])}
            chart.add_series(entry)
        if spec.get("title"):
            chart.set_title(
                {
                    "name": str(spec["title"]),
                    "name_font": {"size": 13, "bold": True, "color": "#" + t.text},
                }
            )
        else:
            chart.set_title({"none": True})
        if kind not in ("pie", "doughnut"):
            values = {
                "major_gridlines": {"visible": True, "line": {"color": "#" + t.border}},
                "line": {"none": True},
                "num_font": {"color": "#" + t.muted},
            }
            if spec.get("number_format"):
                values["num_format"] = str(spec["number_format"])
            labels = {"line": {"color": "#" + t.border}, "num_font": {"color": "#" + t.muted}}
            if kind == "bar":
                # En barras horizontales XlsxWriter llama x al eje de valores; las
                # categorías se leen de arriba abajo y el eje de valores queda abajo.
                labels.update(reverse=True, crossing="max")
                chart.set_x_axis(values)
                chart.set_y_axis(labels)
            else:
                chart.set_y_axis(values)
                chart.set_x_axis(labels)
        chart.set_legend(
            {"position": "bottom"}
            if len(series) > 1 or kind in ("pie", "doughnut")
            else {"none": True}
        )
        chart.set_chartarea({"border": {"none": True}})
        chart.set_size(
            {"width": int(spec.get("width") or 640), "height": int(spec.get("height") or 360)}
        )
        anchor = str(spec.get("anchor") or "H2")
        if not _CELL.match(anchor):
            raise _invalid(f"{sheet_name}: bad chart anchor")
        sheet.insert_chart(anchor, chart, {"object_position": 1})
        plan.charts += 1

    def _validation(self, sheet, sheet_name: str, spec: Any) -> None:
        spec = _closed(
            spec,
            frozenset({"range", "list", "between", "type", "message"}),
            f"{sheet_name}: validation",
        )
        ref = str(spec.get("range") or "")
        if not _RANGE.match(ref):
            raise _invalid(f"{sheet_name}: validation range like C4:C100")
        options: dict[str, Any]
        if isinstance(spec.get("list"), list):
            options = {"validate": "list", "source": [str(v) for v in spec["list"]][:200]}
        elif isinstance(spec.get("between"), list) and len(spec["between"]) == 2:
            kind = spec.get("type", "decimal")
            if kind not in ("decimal", "integer", "date"):
                raise _invalid(f"{sheet_name}: validation type decimal|integer|date")
            low, high = spec["between"]
            if kind == "date":
                low, high = _date(low), _date(high)
            options = {"validate": kind, "criteria": "between", "minimum": low, "maximum": high}
        else:
            raise _invalid(f"{sheet_name}: validation needs list or between")
        if spec.get("message"):
            options["input_message"] = str(spec["message"])[:250]
        sheet.data_validation(ref, options)

    def _conditional(self, sheet, sheet_name: str, spec: Any) -> None:
        spec = _closed(spec, frozenset({"range", "rule", "count"}), f"{sheet_name}: conditional")
        ref = str(spec.get("range") or "")
        if not _RANGE.match(ref):
            raise _invalid(f"{sheet_name}: conditional range like C4:C100")
        rule = spec.get("rule")
        t = self.theme
        if rule == "negative_red":
            options = {
                "type": "cell",
                "criteria": "<",
                "value": 0,
                "format": self.fmt(font_color="#" + t.negative),
            }
        elif rule == "data_bar":
            options = {"type": "data_bar", "bar_color": "#" + t.accent}
        elif rule == "color_scale":
            options = {"type": "3_color_scale"}
        elif rule == "top":
            options = {
                "type": "top",
                "value": int(spec.get("count") or 10),
                "format": self.fmt(bold=True, bg_color="#E8F0FE"),
            }
        else:
            raise _invalid(f"{sheet_name}: rule must be negative_red|data_bar|color_scale|top")
        sheet.conditional_format(ref, options)

    def _print(self, sheet, spec: dict[str, Any]) -> None:
        options = spec.get("print")
        if options is None:
            sheet.set_landscape()
            sheet.fit_to_pages(1, 0)
            return
        options = _closed(
            options, frozenset({"landscape", "fit_width", "repeat_header", "area"}), "print"
        )
        if options.get("landscape", True):
            sheet.set_landscape()
        if options.get("fit_width", True):
            sheet.fit_to_pages(1, 0)
        if options.get("repeat_header"):
            sheet.repeat_rows(0, 3)
        if options.get("area"):
            if not _RANGE.match(str(options["area"])):
                raise _invalid("print.area like A1:H40")
            sheet.print_area(str(options["area"]))


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def _date(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _display(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value if value is not None else "")


def _cells(values: list[Any], columns: list[dict[str, Any]]) -> list[Any]:
    """Valores para add_table: los textos siguen siendo texto (sin fórmulas)."""
    out = []
    for value, column in zip(values, columns, strict=False):
        if column.get("type") in ("date", "datetime", "id"):
            out.append(None)  # se escriben después con su tipo
        elif isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            out.append(value)  # strings_to_formulas=False: queda como texto
        else:
            out.append(value)
    return out


def _without_caches(data: bytes) -> bytes:
    """Ninguna fórmula de un libro recién escrito tiene resultado calculado.

    XlsxWriter guarda 0 como caché en columnas calculadas y totales de tabla;
    sería un valor inventado. Se retira: el resultado queda pendiente hasta
    que un backend certificado calcule el libro.
    """
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    pattern = re.compile(rb"(<f(?:\s[^>]*)?>[^<]*</f>|<f(?:\s[^>]*)?/>)<v>[^<]*</v>")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            part = source.read(info.filename)
            if info.filename.startswith("xl/worksheets/sheet") and info.filename.endswith(".xml"):
                part = pattern.sub(rb"\1", part)
            target.writestr(info, part, zipfile.ZIP_DEFLATED)
    return out.getvalue()


def build(
    spec: dict[str, Any], datasets: dict[str, Any] | None = None
) -> tuple[bytes, list[SheetPlan]]:
    builder = _Builder(spec, datasets)
    data = _without_caches(builder.build())
    return data, builder.plans


def template_catalog() -> dict[str, Any]:
    return {
        "profiles": {
            "rich": "Tables, formulas, native charts, validations, names, print setup",
            "streaming": (
                "Row-by-row export for large data (columns + rows/data only); no tables, "
                "charts or loose cells"
            ),
        },
        "workbook": "title, language, theme, profile, sheets[], names {Name: 'Sheet!$B$2'}",
        "sheet": (
            "name, title?, description?, columns[{header, type, format?, width?, total?, "
            "formula? (table formula like [@Ventas]*[@Precio]), currency?}], rows[[...]] or "
            "data {dataset, sql}, table?, cells[{cell, value|formula, format?, style?, name?}], "
            "freeze?, widths?, charts[], validations[], conditional[], print?, hidden?, notes?"
        ),
        "column_types": list(COLUMN_TYPES),
        "cell_styles": list(CELL_STYLES),
        "charts": (
            f"type {'|'.join(CHART_TYPES)}, title?, categories 'A5:A12', "
            "series[{name, values 'B5:B12'}], anchor 'H2', number_format?"
        ),
        "validations": "range, list[...] or between [min, max] + type decimal|integer|date",
        "conditional": "range, rule negative_red|data_bar|color_scale|top (count)",
    }


__all__ = [
    "CHART_TYPES",
    "PROFILES",
    "build",
    "cell_name",
    "column_letter",
    "template_catalog",
    "validate",
]
