"""Operaciones cerradas del runtime documental.

`REGISTRY` son las que corren en el proceso hijo (worker.py): reciben una
solicitud validada y un directorio de salida, y devuelven `(resultado,
archivos)`. `INSPECT` y `READ` son lecturas acotadas que corren en el Engine
tras la inspección del contenedor. Importar este módulo marca como
disponibles las capacidades que implementa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.documents import capabilities
from rinari.documents.contracts import DocumentError, DocumentErrorCode

Files = dict[str, bytes | Path]


def _render(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import render

    source = Path(request["path"])
    kind = request["kind"]
    pdf, backend = render.to_pdf(source, kind, out)
    count = render.pdf_page_count(pdf)
    pages = request.get("pages") or list(
        range(1, min(count, int(request.get("max_pages") or count)) + 1)
    )
    images = render.pdf_to_png(pdf, pages, max_side=int(request.get("max_side") or 1600))
    files: Files = {"pdf": pdf}
    rows = []
    for image in images:
        files[f"page-{image['page']}"] = image["png"]
        rows.append({"page": image["page"], "width": image["width"], "height": image["height"]})
    return {
        "backend": backend,
        "page_count": count,
        "pages": rows,
    }, files


def _checks(path: Path, expected: list[str] | None = None) -> dict[str, Any]:
    from rinari.documents.validation import pptx_checks

    return {
        "structure": pptx_checks.structure(path).to_dict(),
        "layout": pptx_checks.layout(path).to_dict(),
        "content": pptx_checks.content(path, expected).to_dict(),
    }


def _pptx_create(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import pptx_build

    data, plans = pptx_build.build(request["spec"], request.get("resources") or {})
    target = out / "deck.pptx"
    target.write_bytes(data)
    return {
        "slides": [
            {"id": plan.id, "layout": plan.layout, "elements": len(plan.elements)} for plan in plans
        ],
        "plan_findings": pptx_build.plan_findings(plans),
        "checks": _checks(target, request.get("expected_text")),
    }, {"document": data}


def _pptx_edit(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation
    from rinari.documents.adapters import pptx_edit

    source = Path(request["path"])
    target = out / "edited.pptx"
    state = pptx_edit.apply(source, target, request["operations"], request.get("resources") or {})
    diff = preservation.diff_packages(source, target)
    verdict = preservation.evaluate(
        diff,
        state.allowed,
        request.get("preservation") or preservation.PRESERVE_STRICT,
        allow_new=tuple(sorted(state.allow_new)),
    )
    if verdict["status"] == "failed":
        raise DocumentError(
            DocumentErrorCode.PRESERVATION_RISK,
            "The edit would change parts it did not declare",
            details={"preservation": verdict},
            action="Use a narrower operation or allow a rebuild explicitly",
        )
    return {
        "changes": state.changes,
        "plan_findings": state.findings,
        "preservation": {**verdict, "diff": diff},
        "semantic_diff": preservation.semantic_diff(source, target),
        "checks": _checks(target, request.get("expected_text")),
    }, {"document": target.read_bytes()}


def _pptx_validate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    return {"checks": _checks(Path(request["path"]), request.get("expected_text"))}, {}


def _diff(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation

    before, after = Path(request["before"]), Path(request["after"])
    result: dict[str, Any] = {"package": preservation.diff_packages(before, after)}
    if request.get("kind") == "pptx":
        result["content"] = preservation.semantic_diff(before, after)
    return result, {}


# -- hojas de cálculo --------------------------------------------------------------------
def _xlsx_checks(path: Path, expected: list[str] | None, *, calculated_by: str | None = None):
    from rinari.documents.adapters import xlsx_edit
    from rinari.documents.validation import xlsx_checks

    inventory = xlsx_edit.formulas(path, calculated=calculated_by is not None)
    return {
        "structure": xlsx_checks.structure(path).to_dict(),
        "content": xlsx_checks.content(path, expected).to_dict(),
        "formulas": xlsx_checks.formulas_check(inventory, calculated_by=calculated_by).to_dict(),
    }, inventory


def _xlsx_create(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    import copy

    from rinari.documents.adapters import data_engine, xlsx_build

    spec = copy.deepcopy(request["spec"])
    datasets: dict[str, str] = request.get("datasets") or {}
    sources: dict[str, Any] = {}
    for sheet in spec.get("sheets") or []:
        data = sheet.get("data") if isinstance(sheet, dict) else None
        if not isinstance(data, dict):
            continue
        alias = str(data.get("dataset") or "")
        if alias not in datasets:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown dataset {alias!r}")
        columns, rows = data_engine.rows({alias: datasets[alias]}, str(data.get("sql") or ""))
        if not sheet.get("columns"):
            sheet["columns"] = [
                {"header": c["name"], "type": data_engine.column_type(c["type"])} for c in columns
            ]
        sources[sheet["name"]] = rows
    data, plans = xlsx_build.build(spec, {"__rows__": sources})
    target = out / "book.xlsx"
    target.write_bytes(data)
    checks, inventory = _xlsx_checks(target, request.get("expected_text"))
    return {
        "sheets": [plan.to_dict() for plan in plans],
        "plan_findings": [f for plan in plans for f in plan.findings],
        "checks": checks,
        "formulas": inventory,
    }, {"document": target}


def _xlsx_edit(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation
    from rinari.documents.adapters import xlsx_edit

    source = Path(request["path"])
    target = out / "edited.xlsx"
    policy = request.get("preservation") or preservation.PRESERVE_STRICT
    state = xlsx_edit.apply(
        source,
        target,
        request["operations"],
        rebuild_allowed=policy != preservation.PRESERVE_STRICT,
    )
    diff = preservation.diff_packages(source, target)
    verdict = preservation.evaluate(
        diff, state.allowed, policy, allow_new=tuple(sorted(state.allow_new))
    )
    if verdict["status"] == "failed":
        raise DocumentError(
            DocumentErrorCode.PRESERVATION_RISK,
            "The edit would change or drop parts it did not declare",
            details={"preservation": verdict},
            action="Use set_cells only, or accept the reported loss with rebuild",
        )
    checks, inventory = _xlsx_checks(target, request.get("expected_text"))
    backend = "openpyxl" if state.structural else "ooxml-patch"
    return {
        "changes": state.changes,
        "preservation": {**verdict, "diff": diff, "backend": backend},
        "checks": checks,
        "formulas": inventory,
    }, {"document": target}


def _xlsx_validate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    checks, inventory = _xlsx_checks(
        Path(request["path"]),
        request.get("expected_text"),
        calculated_by=request.get("calculated_by"),
    )
    return {"checks": checks, "formulas": inventory}, {}


def _xlsx_calculate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import excel_calc

    target = excel_calc.calculate(Path(request["path"]), out)
    checks, inventory = _xlsx_checks(
        target, request.get("expected_text"), calculated_by="excel-com"
    )
    return {"checks": checks, "formulas": inventory, "backend": "excel-com"}, {"document": target}


def _dataset_import(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import data_engine

    target = out / "dataset.duckdb"
    profile = data_engine.import_source(
        Path(request["path"]), request["kind"], request.get("options") or {}, target
    )
    return profile, {"dataset": target}


def _dataset_query(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import data_engine

    result = data_engine.query(
        request["datasets"],
        request["sql"],
        limit=int(request.get("limit") or data_engine.DEFAULT_LIMIT),
        into=request.get("into"),
        out_dir=out,
        csv_safe=request.get("csv_safe", True) is not False,
    )
    files: Files = {}
    if result.get("path"):
        key = "dataset" if result.get("into") == "dataset" else "csv"
        files[key] = out / result.pop("path")
    return result, files


# -- Word y PDF ---------------------------------------------------------------------------
def _update_fields(target: Path, out: Path, request: dict[str, Any]) -> tuple[Path, str | None]:
    """Si el documento tiene índice y Word está disponible, Word lo pagina."""
    import zipfile

    from rinari.documents.adapters import word_fields

    with zipfile.ZipFile(target) as archive:
        has_toc = " TOC " in archive.read("word/document.xml").decode("utf-8", "replace")
    if not has_toc or request.get("update_fields") is False or not word_fields.available():
        return target, None
    return word_fields.update(target, out), "word-com"


def _docx_create(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import docx_build, docx_edit
    from rinari.documents.validation import report_checks

    target = out / "document.docx"
    result: dict[str, Any] = {}
    if request.get("template"):
        variables = docx_edit.render_template(
            Path(request["template"]), target, request.get("context") or {}
        )
        result["template_variables"] = variables
    else:
        data, plan = docx_build.build(request["spec"], request.get("resources") or {})
        target.write_bytes(data)
        result["plan"] = {
            "headings": plan.headings,
            "tables": plan.tables,
            "figures": plan.figures,
            "toc": plan.toc,
        }
    final, updated_by = _update_fields(target, out, request)
    result["checks"] = report_checks.docx_checks(
        final, request.get("expected_text"), fields_updated_by=updated_by
    )
    result["fields_backend"] = updated_by
    return result, {"document": final}


def _docx_edit(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents import preservation
    from rinari.documents.adapters import docx_edit
    from rinari.documents.validation import report_checks

    source = Path(request["path"])
    target = out / "edited.docx"
    state = docx_edit.apply(source, target, request["operations"])
    diff = preservation.diff_packages(source, target)
    verdict = preservation.evaluate(
        diff,
        state.allowed,
        request.get("preservation") or preservation.PRESERVE_STRICT,
        allow_new=tuple(sorted(state.allow_new)),
    )
    if verdict["status"] == "failed":
        raise DocumentError(
            DocumentErrorCode.PRESERVATION_RISK,
            "The edit would change parts it did not declare",
            details={"preservation": verdict},
        )
    return {
        "changes": state.changes,
        "preservation": {**verdict, "diff": diff, "backend": "python-docx"},
        "semantic_diff": report_checks.docx_semantic_diff(source, target),
        "checks": report_checks.docx_checks(target, request.get("expected_text")),
    }, {"document": target}


def _docx_validate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.validation import report_checks

    return {
        "checks": report_checks.docx_checks(
            Path(request["path"]),
            request.get("expected_text"),
            fields_updated_by=request.get("fields_updated_by"),
        )
    }, {}


def _pdf_create(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import pdf_build
    from rinari.documents.validation import report_checks

    data, plan = pdf_build.build(request["spec"], request.get("resources") or {})
    target = out / "document.pdf"
    target.write_bytes(data)
    return {
        "plan": {
            "pages": plan.pages,
            "headings": plan.headings,
            "tables": plan.tables,
            "figures": plan.figures,
            "toc": plan.toc,
            "fonts": plan.fonts,
        },
        "checks": report_checks.pdf_checks(target, request.get("expected_text")),
    }, {"document": target}


def _pdf_edit(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.adapters import pdf_edit
    from rinari.documents.validation import report_checks

    source = Path(request["path"])
    target = out / "edited.pdf"
    state = pdf_edit.apply(source, target, request["operations"], request.get("inputs") or {})
    checks = report_checks.pdf_checks(
        target, request.get("expected_text"), filled=state.filled or None
    )
    return {
        "changes": state.changes,
        "filled": state.filled,
        "flattened": state.flattened,
        "semantic_diff": report_checks.pdf_semantic_diff(source, target),
        "checks": checks,
    }, {"document": target}


def _pdf_validate(request: dict[str, Any], out: Path) -> tuple[dict[str, Any], Files]:
    from rinari.documents.validation import report_checks

    return {
        "checks": report_checks.pdf_checks(
            Path(request["path"]), request.get("expected_text"), filled=request.get("filled")
        )
    }, {}


REGISTRY: dict[str, Any] = {
    "docx.create": _docx_create,
    "docx.edit": _docx_edit,
    "docx.validate": _docx_validate,
    "pdf.create": _pdf_create,
    "pdf.edit": _pdf_edit,
    "pdf.validate": _pdf_validate,
    "render": _render,
    "pptx.create": _pptx_create,
    "pptx.edit": _pptx_edit,
    "pptx.validate": _pptx_validate,
    "xlsx.create": _xlsx_create,
    "xlsx.edit": _xlsx_edit,
    "xlsx.validate": _xlsx_validate,
    "xlsx.calculate": _xlsx_calculate,
    "dataset.import": _dataset_import,
    "dataset.query": _dataset_query,
    "diff": _diff,
}


def _inspect_pptx(path: Path) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.inspect(path)


def _read_pptx(path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    from rinari.documents.adapters import pptx_read

    return pptx_read.read(path, selection, cursor)


def _basic(name: str):
    def call(*args):
        from rinari.documents.adapters import basic_read

        return getattr(basic_read, name)(*args)

    return call


INSPECT: dict[str, Any] = {
    "pptx": _inspect_pptx,
    "pdf": _basic("inspect_pdf"),
    "docx": _basic("inspect_docx"),
    "xlsx": _basic("inspect_xlsx"),
}
READ: dict[str, Any] = {
    "pptx": _read_pptx,
    "pdf": _basic("read_pdf"),
    "docx": _basic("read_docx"),
    "xlsx": _basic("read_xlsx"),
}

for _kind in INSPECT:
    capabilities.enable(_kind, "inspect")
for _kind in READ:
    capabilities.enable(_kind, "read")
capabilities.enable("pdf", "render")
capabilities.enable("pptx", "create")
capabilities.enable("pptx", "edit")
capabilities.enable("xlsx", "create")
capabilities.enable("xlsx", "edit")
capabilities.enable("xlsx", "query")
capabilities.enable("xlsx", "calculate")
capabilities.enable("docx", "create")
capabilities.enable("docx", "edit")
capabilities.enable("pdf", "create")
capabilities.enable("pdf", "edit")


def register(name: str, function: Any, *, kind: str | None = None, operation: str | None = None):
    REGISTRY[name] = function
    if kind and operation:
        capabilities.enable(kind, operation)


def inspect(kind: str, path: Path) -> dict[str, Any]:
    function = INSPECT.get(kind)
    if function is None:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot inspect {kind} yet")
    return function(path)


def read(kind: str, path: Path, selection: str | None, cursor: int | None) -> dict[str, Any]:
    function = READ.get(kind)
    if function is None:
        raise DocumentError(DocumentErrorCode.UNSUPPORTED_FEATURE, f"Cannot read {kind} yet")
    return function(path, selection, cursor)
