"""Entrega 3: hojas empresariales y datos pesados.

Libros con XlsxWriter sin cachés inventadas, edición de celdas que no toca el
resto del paquete, datasets DuckDB consultables sin acceso externo y
recálculo solo con un backend certificado.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import zipfile

import pytest

from rinari.artifacts.store import ArtifactStore
from rinari.documents import preservation
from rinari.documents.adapters import data_engine, excel_calc, xlsx_build, xlsx_edit
from rinari.documents.contracts import DocumentError, DocumentErrorCode
from rinari.documents.jobs import JobManager
from rinari.documents.service import DocumentService
from tests.unit import test_tool_runtime as base
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project
SESSION = "ses_sheets"

BOOK = {
    "title": "Presupuesto",
    "sheets": [
        {
            "name": "Resumen",
            "title": "Presupuesto 2027",
            "cells": [
                {"cell": "A4", "value": "Crecimiento", "style": "label"},
                {
                    "cell": "B4",
                    "value": 0.08,
                    "format": "0.0%",
                    "style": "input",
                    "name": "Crecimiento",
                },
                {"cell": "B5", "formula": "=SUM(Ventas[Ventas])", "style": "output"},
                {"cell": "B6", "formula": "=B5*(1+Crecimiento)", "style": "output"},
            ],
            "charts": [
                {
                    "type": "column",
                    "categories": "A9:A10",
                    "series": [{"name": "V", "values": "B9:B10"}],
                }
            ],
        },
        {
            "name": "Ventas",
            "columns": [
                {"header": "Región", "type": "text"},
                {"header": "Código", "type": "id"},
                {"header": "Ventas", "type": "currency", "total": "sum"},
                {"header": "Doble", "type": "number", "formula": "[@Ventas]*2"},
            ],
            "rows": [["Norte", "000123", 10.5, None], ['=HYPERLINK("x")', "000456", 4, None]],
            "table": {"name": "Ventas"},
        },
    ],
}


@pytest.fixture
def store(app_ctx, monkeypatch):
    monkeypatch.setenv("RINARI_DOCUMENTS_NO_OFFICE", "1")
    monkeypatch.setenv("RINARI_DOCUMENTS_NO_LIBREOFFICE", "1")
    yield ArtifactStore(app_ctx)
    JobManager.close_for(app_ctx)


def _sheet_xml(data: bytes, index: int) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read(f"xl/worksheets/sheet{index}.xml").decode()


# -- autoría -------------------------------------------------------------------------


def test_new_workbooks_never_carry_invented_formula_results(tmp_path):
    data, plans = xlsx_build.build(BOOK)
    path = tmp_path / "book.xlsx"
    path.write_bytes(data)
    inventory = xlsx_edit.formulas(path)
    assert inventory["formulas"] >= 4 and inventory["cached"] == 0
    assert inventory["cache_state"] == "missing"
    assert {p.name: p.charts for p in plans} == {"Resumen": 1, "Ventas": 0}


def test_untrusted_text_is_never_a_formula_and_ids_keep_their_zeros(tmp_path):
    import openpyxl

    data, _ = xlsx_build.build(BOOK)
    path = tmp_path / "book.xlsx"
    path.write_bytes(data)
    sheet = openpyxl.load_workbook(path)["Ventas"]
    assert sheet["A3"].value == '=HYPERLINK("x")' and sheet["A3"].data_type == "s"
    assert sheet["B2"].value == "000123"


def test_workbook_specs_are_closed():
    with pytest.raises(DocumentError) as err:
        xlsx_build.build({"sheets": [{"name": "A", "colour": "red"}]})
    assert err.value.code is DocumentErrorCode.INVALID_SPEC
    with pytest.raises(DocumentError):
        xlsx_build.build({"sheets": [{"name": "Bad/Name"}]})
    streaming = {"profile": "streaming", "sheets": [{"name": "A", "charts": []}]}
    with pytest.raises(DocumentError) as err:
        xlsx_build.build(streaming)
    assert "charts" in err.value.message


def test_streaming_profile_writes_rows_in_bounded_memory(tmp_path):
    rows = ([i, f"fila {i}", i * 0.5] for i in range(20_000))
    spec = {
        "profile": "streaming",
        "sheets": [
            {
                "name": "Datos",
                "columns": [
                    {"header": "n", "type": "integer"},
                    {"header": "texto"},
                    {"header": "valor", "type": "number"},
                ],
                "data": {"dataset": "x", "sql": "select 1"},
            }
        ],
    }
    data, plans = xlsx_build.build(spec, {"__rows__": {"Datos": rows}})
    assert plans[0].rows == 20_000
    assert "<tableParts" not in _sheet_xml(data, 1)


# -- edición --------------------------------------------------------------------------


@pytest.fixture
def book(tmp_path):
    data, _ = xlsx_build.build(BOOK)
    path = tmp_path / "book.xlsx"
    path.write_bytes(data)
    return path


def test_cell_edits_touch_only_the_sheet_even_with_charts(book, tmp_path):
    out = tmp_path / "out.xlsx"
    state = xlsx_edit.apply(
        book,
        out,
        [
            {
                "op": "xlsx.set_cells",
                "sheet": "Resumen",
                "cells": [
                    {"cell": "B4", "value": 0.1, "expected": 0.08},
                    {"cell": "C6", "formula": "=B6-B5"},
                    {"cell": "A20", "value": "=texto, no fórmula"},
                ],
            }
        ],
        rebuild_allowed=False,
    )
    diff = preservation.diff_packages(book, out)
    changed = {row["part"] for row in diff["changed"]}
    assert changed <= {"xl/worksheets/sheet1.xml", "xl/workbook.xml"}
    assert preservation.evaluate(diff, state.allowed, "preserve_strict")["status"] == "passed"
    xml = _sheet_xml(out.read_bytes(), 1)
    assert "<f>B6-B5</f>" in xml and "=texto, no fórmula" in xml
    assert "fullCalcOnLoad" in zipfile.ZipFile(out).read("xl/workbook.xml").decode()


def test_cell_edit_preconditions_and_protected_cells(book, tmp_path):
    with pytest.raises(DocumentError) as err:
        xlsx_edit.apply(
            book,
            tmp_path / "o.xlsx",
            [
                {
                    "op": "xlsx.set_cells",
                    "sheet": "Resumen",
                    "cells": [{"cell": "B4", "value": 1, "expected": 0.5}],
                }
            ],
            rebuild_allowed=False,
        )
    assert err.value.code is DocumentErrorCode.REVISION_CONFLICT
    with pytest.raises(DocumentError) as err:
        xlsx_edit.apply(
            book,
            tmp_path / "o.xlsx",
            [{"op": "xlsx.set_cells", "sheet": "Ventas", "cells": [{"cell": "C1", "value": "x"}]}],
            rebuild_allowed=False,
        )
    assert err.value.code is DocumentErrorCode.UNSUPPORTED_FEATURE


def test_structure_changes_need_an_explicit_policy_and_report_losses(book, tmp_path):
    operations = [{"op": "xlsx.add_sheet", "name": "Notas", "rows": [["Hola"]]}]
    with pytest.raises(DocumentError) as err:
        xlsx_edit.apply(book, tmp_path / "o.xlsx", operations, rebuild_allowed=False)
    assert err.value.code is DocumentErrorCode.PRESERVATION_RISK
    out = tmp_path / "o.xlsx"
    state = xlsx_edit.apply(book, out, operations, rebuild_allowed=True)
    verdict = preservation.evaluate(
        preservation.diff_packages(book, out), state.allowed, "preserve_best_effort"
    )
    # openpyxl reescribe el libro: nada se da por intacto, todo cambio queda declarado.
    assert verdict["status"] == "partial" and verdict["unexpected"]


def test_losing_charts_or_pivots_is_never_compatible():
    diff = {
        "added": [],
        "changed": [{"part": "xl/worksheets/sheet1.xml", "category": "worksheet"}],
        "removed": [
            {"part": "xl/charts/chart1.xml", "category": "chart"},
            {"part": "xl/pivotTables/pivotTable1.xml", "category": "pivot"},
        ],
        "unchanged": 10,
    }
    verdict = preservation.evaluate(diff, set(), "preserve_best_effort")
    assert verdict["status"] == "failed"
    assert {row["category"] for row in verdict["risky"]} == {"chart", "pivot"}


# -- datos ------------------------------------------------------------------------------


@pytest.fixture
def ventas_csv(tmp_path):
    path = tmp_path / "ventas.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fecha", "region", "codigo", "ventas"])
        for i in range(3_000):
            writer.writerow(
                [
                    f"2026-{i % 12 + 1:02d}-01",
                    ["Norte", "Sur", "=cmd"][i % 3],
                    f"00{i:05d}",
                    i * 1.5,
                ]
            )
    return path


def test_datasets_query_inside_a_locked_engine(ventas_csv, tmp_path):
    db = tmp_path / "ds.duckdb"
    profile = data_engine.import_source(ventas_csv, "csv", {"types": {"codigo": "VARCHAR"}}, db)
    assert profile["row_count"] == 3_000
    assert {c["name"]: c["type"] for c in profile["columns"]}["codigo"] == "VARCHAR"
    result = data_engine.query(
        {"ventas": str(db)},
        "select region, count(*) n from data group by 1 order by 1",
        out_dir=tmp_path,
    )
    assert [r[1] for r in result["rows"]] == [1_000, 1_000, 1_000]
    for sql in (
        "select 1; select 2",
        "COPY data TO 'x.csv'",
        "select * from read_csv('C:/Windows/win.ini')",
        "ATTACH 'otra.db' AS o",
    ):
        with pytest.raises(DocumentError):
            data_engine.query({"ventas": str(db)}, sql, out_dir=tmp_path)


def test_csv_exports_neutralize_formula_injection(ventas_csv, tmp_path):
    db = tmp_path / "ds.duckdb"
    data_engine.import_source(ventas_csv, "csv", {}, db)
    result = data_engine.query(
        {"ventas": str(db)}, "select region from data limit 3", into="csv", out_dir=tmp_path
    )
    lines = (tmp_path / result["path"]).read_text(encoding="utf-8-sig").splitlines()
    assert lines[3] == "'=cmd"


def test_service_imports_queries_and_builds_a_workbook_from_a_dataset(store, ventas_csv):
    service = DocumentService(store, SESSION)
    uri = store.create(
        SESSION, "media", "ventas.csv", ventas_csv.read_bytes(), content_type="text/csv"
    ).uri()
    job = service.wait(service.import_dataset(uri), 120)
    assert job["status"] == "succeeded", job["error"]
    dataset = job["result"]["dataset"]
    assert dataset["row_count"] == 3_000 and dataset["alias"] == "ventas"
    again = service.import_dataset(uri)
    assert again["result"]["reused"] is True

    totals = service.wait(
        service.query(
            [dataset["id"]],
            "select region, sum(ventas) total from data group by 1",
            into="dataset",
            name="totales",
        ),
        120,
    )
    assert totals["status"] == "succeeded", totals["error"]
    derived = totals["result"]["dataset"]
    assert derived["row_count"] == 3 and derived["parent_id"] == dataset["id"]

    spec = {
        "kind": "xlsx",
        "title": "Ventas por región",
        "sheets": [
            {
                "name": "Totales",
                "data": {"dataset": "totales", "sql": "select * from data order by total desc"},
            }
        ],
    }
    built = service.wait(service.create(spec), 120)
    assert built["status"] == "succeeded", built["error"]
    report = built["result"]["report"]
    assert report["checks"]["structure"]["status"] == "passed"
    assert built["result"]["report"]["sheets"][0]["rows"] == 3


def test_uncalculated_formulas_are_not_claimed(store):
    service = DocumentService(store, SESSION)
    job = service.wait(service.create({**BOOK, "kind": "xlsx"}), 120)
    assert job["status"] == "succeeded", job["error"]
    report = job["result"]["report"]
    assert report["checks"]["formulas"]["status"] == "not_run"
    assert report["checks"]["formulas"]["reason"] == "NOT_CALCULATED"
    revision = job["result"]["revision"]["id"]
    pending = service.finalize(revision)
    assert pending["finalized"] is False
    assert any(item["check"] == "formulas" for item in pending["pending"])
    with pytest.raises(DocumentError) as err:
        service.calculate(revision)
    assert err.value.code is DocumentErrorCode.CALCULATION_BACKEND_UNAVAILABLE


def test_spreadsheet_edits_through_the_service(store):
    service = DocumentService(store, SESSION)
    parent = service.wait(service.create({**BOOK, "kind": "xlsx"}), 120)["result"]["revision"]
    job = service.wait(
        service.edit(
            parent["id"],
            [
                {
                    "op": "xlsx.set_cells",
                    "sheet": "Resumen",
                    "cells": [{"cell": "B4", "value": 0.12}],
                }
            ],
        ),
        120,
    )
    assert job["status"] == "succeeded", job["error"]
    child = job["result"]["revision"]
    assert child["backend"] == "ooxml-patch" and child["parent_id"] == parent["id"]
    assert job["result"]["report"]["checks"]["preservation"]["status"] == "passed"


# -- herramientas -------------------------------------------------------------------------


def test_query_tool_imports_a_project_file_and_answers_without_loading_it(
    store, app_ctx, tmp_path, ventas_csv
):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "ventas.csv").write_bytes(ventas_csv.read_bytes())
    ctx = dataclasses.replace(
        _ctx(tmp_path, root), artifact_root=store._root(), artifact_store=store
    )
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    described = runtime.execute("spreadsheets.query", {"source": "ventas.csv"}, ctx)
    assert described.ok, described.error
    assert described.data["datasets"][0]["row_count"] == 3_000
    answer = runtime.execute(
        "spreadsheets.query",
        {"datasets": ["ventas"], "sql": "select count(*) n from data where region = 'Norte'"},
        ctx,
    )
    assert answer.ok and answer.data["result"]["rows"] == [[1_000]]
    bad = runtime.execute(
        "spreadsheets.query", {"datasets": ["ventas"], "sql": "drop table data"}, ctx
    )
    assert not bad.ok and bad.error.message.startswith("INVALID_SPEC")


@pytest.mark.skipif(not excel_calc.available(), reason="no Excel")
def test_excel_recalculates_and_reports_fresh_results(app_ctx):
    store = ArtifactStore(app_ctx)
    try:
        service = DocumentService(store, SESSION)
        parent = service.wait(service.create({**BOOK, "kind": "xlsx"}, render=False), 120)[
            "result"
        ]["revision"]
        job = service.wait(service.calculate(parent["id"]), 300)
        assert job["status"] == "succeeded", job["error"]
        report = job["result"]["report"]
        assert report["checks"]["formulas"]["status"] == "passed"
        assert report["formulas"]["cache_state"] == "fresh"
        assert job["result"]["revision"]["operation"] == "calculate"
    finally:
        JobManager.close_for(app_ctx)


def test_macro_workbooks_are_detected_and_never_edited_as_xlsx(store, tmp_path):
    """Gate A5: un XLSM se reconoce, no se ejecuta y no se edita como si fuera XLSX."""
    import xlsxwriter

    vba = tmp_path / "vbaProject.bin"
    vba.write_bytes(b"\xd0\xcf\x11\xe0" + b"0" * 600)
    path = tmp_path / "modelo.xlsm"
    book = xlsxwriter.Workbook(str(path))
    book.add_worksheet().write("A1", 1)
    book.add_vba_project(str(vba))
    book.close()
    service = DocumentService(store, SESSION)
    uri = store.create(SESSION, "media", "modelo.xlsm", path.read_bytes()).uri()
    inspected = service.inspect(uri)
    assert inspected["editable"] is False and inspected["reason"] == "UNSUPPORTED_FORMAT"
    assert any(risk["code"] == "macros" for risk in inspected["container"]["risks"])
    with pytest.raises(DocumentError) as err:
        service.edit(
            inspected["revision"]["id"],
            [{"op": "xlsx.set_cells", "sheet": "Sheet1", "cells": [{"cell": "A1", "value": 2}]}],
        )
    assert err.value.code in (
        DocumentErrorCode.UNSUPPORTED_FEATURE,
        DocumentErrorCode.UNSUPPORTED_FORMAT,
    )


BUDGET = {
    "kind": "xlsx",
    "title": "Presupuesto con escenarios",
    "sheets": [
        {
            "name": "Supuestos",
            "cells": [
                {"cell": "A1", "value": "Escenario (1-3)", "style": "label"},
                {"cell": "B1", "value": 2, "style": "input", "name": "Escenario"},
                {"cell": "A3", "value": "Crecimiento", "style": "label"},
                {"cell": "B3", "value": 0.02, "style": "input"},
                {"cell": "C3", "value": 0.05, "style": "input"},
                {"cell": "D3", "value": 0.09, "style": "input"},
                {"cell": "A4", "value": "Base 2026", "style": "label"},
                {"cell": "B4", "value": 1000, "style": "input", "name": "Base"},
            ],
        },
        {
            "name": "Resultado",
            "cells": [
                {"cell": "A1", "value": "Crecimiento elegido", "style": "label"},
                {
                    "cell": "B1",
                    "formula": "=CHOOSE(Escenario,Supuestos!B3,Supuestos!C3,Supuestos!D3)",
                    "name": "Crecimiento",
                },
                {"cell": "A2", "value": "Ventas 2027", "style": "label"},
                {"cell": "B2", "formula": "=Base*(1+Crecimiento)", "style": "output"},
                {"cell": "A3", "value": "Ventas 2028", "style": "label"},
                {"cell": "B3", "formula": "=B2*(1+Crecimiento)", "style": "output"},
            ],
        },
    ],
}


@pytest.mark.skipif(not excel_calc.available(), reason="no Excel")
def test_budget_scenarios_are_edited_and_recalculated_by_excel(app_ctx):
    """Gate A4: cambiar el escenario y un supuesto, recalcular y comprobar los resultados."""
    store = ArtifactStore(app_ctx)
    try:
        service = DocumentService(store, SESSION)
        created = service.wait(service.create(BUDGET, render=False), 120)
        assert created["status"] == "succeeded", created["error"]
        edited = service.wait(
            service.edit(
                created["result"]["revision"]["id"],
                [
                    {
                        "op": "xlsx.set_cells",
                        "sheet": "Supuestos",
                        "cells": [
                            {"cell": "B1", "value": 3, "expected": 2},
                            {"cell": "D3", "value": 0.1, "expected": 0.09},
                        ],
                    }
                ],
                render=False,
            ),
            120,
        )
        assert edited["status"] == "succeeded", edited["error"]
        assert edited["result"]["report"]["checks"]["preservation"]["status"] == "passed"
        job = service.wait(service.calculate(edited["result"]["revision"]["id"]), 300)
        assert job["status"] == "succeeded", job["error"]
        assert job["result"]["report"]["checks"]["formulas"]["status"] == "passed"
        values = service.read(job["result"]["revision"]["id"], "Resultado!A1:B3")
        cached = {c["cell"]: c.get("cached") for row in values["rows"] for c in row["cells"]}
        assert cached["B1"] == pytest.approx(0.1)
        assert cached["B2"] == pytest.approx(1100)
        assert cached["B3"] == pytest.approx(1210)
    finally:
        JobManager.close_for(app_ctx)


def _excel_pids() -> set[int]:
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"EXCEL')}


@pytest.mark.skipif(not excel_calc.available(), reason="no Excel")
def test_cancelling_a_calculation_ends_only_its_own_excel(app_ctx):
    """Gate A11: cancelar durante el cálculo y reiniciar no deja nada a medias."""
    import time

    store = ArtifactStore(app_ctx)
    try:
        service = DocumentService(store, SESSION)
        parent = service.wait(service.create({**BOOK, "kind": "xlsx"}, render=False), 120)
        parent = parent["result"]["revision"]
        before = _excel_pids()
        job = service.calculate(parent["id"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (_excel_pids() - before):
            time.sleep(0.1)
        started = _excel_pids() - before
        assert started, "Excel never started"
        service.cancel(job["job_id"])
        final = service.wait(job, 30)
        assert final["status"] == "cancelled"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (started & _excel_pids()):
            time.sleep(0.2)
        assert not (started & _excel_pids()), "the job's Excel is still running"
        assert before <= _excel_pids(), "an Excel that was already running was touched"
        assert service.revisions.get(parent["id"]).sha256 == parent["sha256"]
        assert service.revisions.path(service.revisions.get(parent["id"]))
        JobManager._instances.clear()
        restarted = JobManager.for_context(app_ctx)
        assert restarted.get(job["job_id"], session_id=SESSION)["status"] == "cancelled"
    finally:
        JobManager.close_for(app_ctx)
