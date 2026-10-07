"""Entrega 1: herramientas del modelo, protocolo del escritorio y adjuntos PPTX."""

from __future__ import annotations

import dataclasses
import itertools
import json
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.artifacts.attachments import prepare_attachments
from rinari.artifacts.store import ArtifactStore
from rinari.documents.jobs import JobManager
from rinari.engine_protocol.server import EngineServer
from rinari.tools.definition import ToolErrorCode
from tests.unit import test_tool_runtime as base
from tests.unit.documents import builders
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project


@pytest.fixture(autouse=True)
def _jobs_drained(app_ctx):
    yield
    manager = JobManager.for_context(app_ctx)
    manager._pool.shutdown(wait=True, cancel_futures=True)
    JobManager._instances.clear()


def _tool_runtime(app_ctx, tmp_path):
    store = ArtifactStore(app_ctx)
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    ctx = dataclasses.replace(
        _ctx(tmp_path, root), artifact_root=store._root(), artifact_store=store
    )
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    return store, root, ctx, runtime


def test_document_tools_are_lazy(app_ctx, tmp_path):
    _, _, _, runtime = _tool_runtime(app_ctx, tmp_path)
    names = [n for n in runtime.registry.names() if n.startswith("documents.")]
    assert {"documents.inspect", "documents.read", "documents.render"} <= set(names)
    # No pesan en cada petición: se activan con una skill documental.
    assert all(runtime.registry.get(n).always_loaded is False for n in names)


def test_inspect_a_local_file_imports_it_first(app_ctx, tmp_path):
    _, root, ctx, runtime = _tool_runtime(app_ctx, tmp_path)
    builders.deck(root / "ventas.pptx", slides=2)

    result = runtime.execute("documents.inspect", {"document": "ventas.pptx"}, ctx)

    assert result.ok, result.error
    revision = result.data["revision"]
    assert revision["operation"] == "import" and revision["name"] == "ventas.pptx"
    assert result.data["inspection"]["slide_count"] == 2
    read = runtime.execute("documents.read", {"document": revision["id"], "selection": "2"}, ctx)
    assert read.ok and read.data["slides"][0]["index"] == 2


def test_errors_carry_the_document_code(app_ctx, tmp_path):
    _, root, ctx, runtime = _tool_runtime(app_ctx, tmp_path)
    (root / "nota.txt").write_text("hola", encoding="utf-8")

    result = runtime.execute("documents.inspect", {"document": "nota.txt"}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert result.error.message.startswith("UNSUPPORTED_FORMAT")


def test_render_can_attach_pages_for_visual_review(app_ctx, tmp_path):
    _, root, ctx, runtime = _tool_runtime(app_ctx, tmp_path)
    builders.pdf(root / "informe.pdf", pages=3)

    result = runtime.execute(
        "documents.render", {"document": "informe.pdf", "show": "2", "wait_s": 120}, ctx
    )

    assert result.ok, result.error
    assert result.data["status"] == "succeeded"
    assert len(result.images) == 1 and len(result.artifacts) == 3


# -- protocolo ---------------------------------------------------------------------


@pytest.fixture
def server(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    services = build_services(app_ctx, user_home=home)
    services.providers.add(
        AddProviderInput(
            alias="fake", provider_type="openai", endpoint="http://127.0.0.1:9/v1", secret="x"
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    engine = EngineServer(services, user_home=home)
    yield services, engine
    engine.close()


_ids = itertools.count()


def _call(engine, method, params):
    request = {"id": f"{method}-{next(_ids)}", "method": method, "params": params}
    return engine.handle_line(json.dumps(request))


def test_desktop_starts_a_render_and_hears_its_progress(server, tmp_path):
    services, engine = server
    session = _call(engine, "session.create", {"cwd": str(tmp_path)})["result"]["session"]["id"]
    path = builders.pdf(tmp_path / "informe.pdf", pages=2)
    uri = services.artifacts.create(session, "media", "informe.pdf", path.read_bytes()).uri()
    events = []
    engine._turns.add_observer(lambda payload: events.append(payload))

    started = _call(
        engine,
        "documents.job.start",
        {"session_id": session, "operation": "render", "ref": uri},
    )
    assert started["ok"], started
    job_id = started["result"]["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = _call(engine, "documents.job.get", {"session_id": session, "job_id": job_id})
        assert job["ok"], job
        if job["result"]["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert job["result"]["status"] == "succeeded", job
    assert any(e.get("event") == "document.job.updated" for e in events)
    revision_id = job["result"]["result"]["revision_id"]
    preview = _call(engine, "documents.preview.get", {"session_id": session, "ref": revision_id})
    assert len(preview["result"]["preview"]["pages"]) == 2
    listed = _call(engine, "documents.revisions.list", {"session_id": session})
    assert listed["result"]["revisions"][0]["id"] == revision_id


def test_desktop_job_operations_are_closed(server, tmp_path):
    _, engine = server
    session = _call(engine, "session.create", {"cwd": str(tmp_path)})["result"]["session"]["id"]
    bad = _call(
        engine,
        "documents.job.start",
        {"session_id": session, "operation": "shell", "ref": "rev_x"},
    )
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"


# -- adjuntos ------------------------------------------------------------------------


def test_pptx_attachment_brings_text_per_slide(app_ctx, tmp_path):
    path = builders.deck(tmp_path / "ventas.pptx", slides=2)
    item = prepare_attachments(ArtifactStore(app_ctx), "ses_a", [str(path)])[0]
    assert item.kind == "pptx"
    assert "[Slide 1]" in item.extracted_text and "[Slide 2]" in item.extracted_text
    assert "[Notes] Nota 1" in item.extracted_text
    assert item.truncated is False


def test_xlsx_cell_limit_is_declared(app_ctx, tmp_path, monkeypatch):
    # Antes el recorte solo estaba en el texto y el adjunto parecía completo.
    from openpyxl import Workbook

    from rinari.artifacts import attachments

    workbook = Workbook()
    for row in range(1, 40):
        workbook.active.append([row, row * 2, row * 3])
    path = tmp_path / "grande.xlsx"
    workbook.save(path)
    monkeypatch.setattr(attachments, "MAX_XLSX_CELLS", 30)

    item = prepare_attachments(ArtifactStore(app_ctx), "ses_a", [str(path)])[0]

    assert item.truncated is True
    assert "Only the first 30 cells were read" in item.warning
