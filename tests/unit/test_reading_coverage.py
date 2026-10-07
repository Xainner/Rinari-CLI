"""What a document preparation read, and seeing pages the text misses.

"OCR used" never meant "document read": a PDF is prepared up to 20 pages,
OCR can find nothing, run out of budget or be missing. Coverage counts each
case from what actually happened; fs.read_pdf_pages shows any page as an
image; an image can go to the model as OCR text and pixels together.
"""

from __future__ import annotations

import dataclasses
import itertools

import pypdfium2 as pdfium
import pytest
from PIL import Image

from rinari.artifacts import attachments
from rinari.artifacts.attachments import attachment_prompt, prepare_attachments
from rinari.artifacts.ocr import OcrUnavailableError
from rinari.artifacts.store import ArtifactStore
from rinari.tools.definition import ToolErrorCode
from tests.unit import test_tool_runtime as base
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project


def _blank_pdf(path, pages):
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(80, 80).close()
    document.save(path)
    document.close()
    return path


def test_pdf_coverage_counts_each_outcome(app_ctx, tmp_path, monkeypatch):
    path = _blank_pdf(tmp_path / "escaneo.pdf", 24)
    outcomes = itertools.chain(["Factura 1", ""], itertools.repeat(None))

    def fake_ocr(*args, **kwargs):
        value = next(outcomes)
        if value is None:
            raise OcrUnavailableError("tesseract missing")
        return value

    monkeypatch.setattr(attachments, "run_ocr", fake_ocr)
    store = ArtifactStore(app_ctx)

    item = prepare_attachments(store, "ses_cov", [str(path)])[0]

    coverage = item.coverage
    assert coverage["total_pages"] == 24 and coverage["prepared_pages"] == 20
    assert (coverage["ocr_pages"], coverage["empty_pages"], coverage["failed_pages"]) == (1, 1, 18)
    assert coverage["pages"][0] == {"page": 1, "method": "ocr"}
    assert coverage["pages"][1] == {"page": 2, "method": "empty"}
    assert coverage["pages"][2] == {"page": 3, "method": "failed", "reason": "ocr_unavailable"}
    assert item.reference()["coverage"] == coverage
    summary = attachment_prompt([item])
    assert "pages read: 20 of 24 (1 OCR, 1 no text found, 18 OCR failed)" in summary
    assert "4 not prepared (fs.read_pdf_pages shows any page)" in summary

    # The cached preparation keeps its coverage.
    again = prepare_attachments(store, "ses_cov", [item.reference()])[0]
    assert again.coverage == coverage


def test_ocr_budget_exhaustion_is_unprocessed_not_failed(app_ctx, tmp_path, monkeypatch):
    path = _blank_pdf(tmp_path / "largo.pdf", 3)
    monkeypatch.setattr(attachments, "run_ocr", lambda *a, **kw: "texto")
    monkeypatch.setattr(attachments, "MAX_OCR_PAGES", 1)

    item = prepare_attachments(ArtifactStore(app_ctx), "ses_cov", [str(path)])[0]

    assert item.coverage["ocr_pages"] == 1
    assert item.coverage["unprocessed_pages"] == 2
    assert item.coverage["pages"][1]["reason"] == "ocr_limit"


def test_text_and_image_keeps_the_pixels(app_ctx, tmp_path, monkeypatch):
    path = tmp_path / "captura.png"
    Image.new("RGB", (32, 32), "white").save(path)
    monkeypatch.setattr(attachments, "run_ocr", lambda *a, **kw: "Error 404")
    store = ArtifactStore(app_ctx)

    text_only = prepare_attachments(store, "ses_cov", [{"path": str(path), "ocr": True}])[0]
    both = prepare_attachments(
        store, "ses_cov", [{"path": str(path), "ocr": True, "keep_image": True}]
    )[0]

    assert text_only.images == () and text_only.extracted_text == "Error 404"
    assert both.images and both.extracted_text == "Error 404" and both.ocr
    assert both.reference()["keep_image"] is True
    prompt = attachment_prompt([both])
    assert "sent as image #1 of this message; text below (OCR)" in prompt
    # Round trip through the reference the client sends back at turn start.
    again = prepare_attachments(store, "ses_cov", [both.reference()])[0]
    assert again.images == both.images


def _runtime_with_store(app_ctx, tmp_path):
    store = ArtifactStore(app_ctx)
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    ctx = _ctx(tmp_path, root)
    ctx = dataclasses.replace(ctx, artifact_root=store._root(), artifact_store=store)
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    return store, root, ctx, runtime


def test_read_pdf_pages_shows_pages_beyond_the_prepared_twenty(app_ctx, tmp_path):
    _store, root, ctx, runtime = _runtime_with_store(app_ctx, tmp_path)
    _blank_pdf(root / "manual.pdf", 25)

    result = runtime.execute("fs.read_pdf_pages", {"path": "manual.pdf", "pages": "22-23"}, ctx)

    assert result.ok, result.error
    assert result.data["page_count"] == 25
    assert [row["page"] for row in result.data["pages"]] == [22, 23]
    assert len(result.images) == 2
    assert all(ref.encoded() for ref in result.images)


def test_read_pdf_pages_takes_an_attached_pdf_and_bounds_the_call(app_ctx, tmp_path):
    store, _root, ctx, runtime = _runtime_with_store(app_ctx, tmp_path)
    attached = prepare_attachments(
        store, ctx.session_id, [str(_blank_pdf(tmp_path / "adj.pdf", 6))]
    )[0]
    uri = attached.source.uri()

    ok = runtime.execute("fs.read_pdf_pages", {"path": uri, "pages": "6"}, ctx)
    many = runtime.execute("fs.read_pdf_pages", {"path": uri, "pages": "1-5"}, ctx)
    outside = runtime.execute("fs.read_pdf_pages", {"path": uri, "pages": "7"}, ctx)

    assert ok.ok and ok.data["pages"][0]["page"] == 6
    assert many.error.code is ToolErrorCode.INVALID_ARGUMENT and "At most 4" in many.error.message
    assert outside.error.code is ToolErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("name", ["foto.png", "nota.txt"])
def test_read_pdf_pages_rejects_other_files(app_ctx, tmp_path, name):
    _store, root, ctx, runtime = _runtime_with_store(app_ctx, tmp_path)
    target = root / name
    if name.endswith(".png"):
        Image.new("RGB", (8, 8)).save(target)
    else:
        target.write_text("hola", encoding="utf-8")

    result = runtime.execute("fs.read_pdf_pages", {"path": name, "pages": "1"}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
