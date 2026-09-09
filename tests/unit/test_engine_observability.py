"""Engine Protocol slice 10: artifacts / context / usage reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.artifacts.store import ArtifactURIError
from rinari.engine_protocol.server import EngineServer
from rinari.storage.records import SessionEventRecord


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    container.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    container.models.add("fake", "fake-model-1", "fake-one")
    container.providers.use("fake")
    return container


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def _req(request_id, method, params=None):
    line: dict = {"id": request_id, "method": method}
    if params is not None:
        line["params"] = params
    return json.dumps(line)


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _err(response):
    assert response is not None and response["ok"] is False, response
    return response["error"]


def _make_session(server, tag, tmp_path):
    created = _ok(server.handle_line(_req(f"{tag}-c", "session.create", {"cwd": str(tmp_path)})))
    return created["session"]["id"]


def test_artifact_list_and_read(services, server, tmp_path) -> None:
    session_id = _make_session(server, "a", tmp_path)
    record = services.artifacts.create_text(
        session_id, "notes", "plan.md", "hello artifact", summary="demo"
    )
    listed = _ok(server.handle_line(_req("a1", "artifact.list", {"session_id": session_id})))[
        "artifacts"
    ]
    assert len(listed) == 1
    assert listed[0]["uri"] == record.uri()
    assert listed[0]["sha256"] == record.sha256
    # No internal filesystem paths leak into the view.
    assert "storage_path" not in listed[0]

    read = _ok(server.handle_line(_req("a2", "artifact.read", {"uri": record.uri()})))
    assert read["text"] == "hello artifact"
    assert read["truncated"] is False

    small = _ok(
        server.handle_line(_req("a3", "artifact.read", {"uri": record.uri(), "max_bytes": 5}))
    )
    assert small["text"] == "hello"
    assert small["truncated"] is True

    assert (
        _err(server.handle_line(_req("a4", "artifact.read", {"uri": "artifact://nope/n/x"})))[
            "code"
        ]
        == "NOT_FOUND"
    )


def test_context_and_usage(services, server, tmp_path) -> None:
    session_id = _make_session(server, "u", tmp_path)
    ctx = _ok(server.handle_line(_req("u1", "context.get", {"ref": session_id})))["context"]
    assert ctx["compacted"] is False
    assert ctx["counts"]["tasks_completed"] == 0

    repo = services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id="evt-1",
            session_id=session_id,
            seq=1,
            type="ModelInvoked",
            payload={
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "stop_reason": "stop",
            },
            created_at="2026-01-01T00:00:00Z",
        )
    )
    repo.insert(
        SessionEventRecord(
            id="evt-2",
            session_id=session_id,
            seq=2,
            type="ToolCompleted",
            payload={"tool_call_id": "c1", "name": "artifact.read", "ok": True},
            created_at="2026-01-01T00:00:00Z",
        )
    )
    usage = _ok(server.handle_line(_req("u2", "usage.get", {"ref": session_id})))["usage"]
    assert usage["model_calls"] == 1
    assert usage["tokens"]["input"] == 100
    assert usage["tokens"]["output"] == 20
    assert usage["tool_calls"] == {"total": 1, "ok": 1, "error": 0}
    # Cost is never invented.
    assert usage["cost"] is None


def test_artifact_export_writes_bytes_inside_dest_dir(services, server, tmp_path) -> None:
    session_id = _make_session(server, "x", tmp_path)
    record = services.artifacts.create_text(session_id, "notes", "plan.md", "hello artifact")
    dest = tmp_path / "out"
    dest.mkdir()

    result = _ok(
        server.handle_line(
            _req("x1", "artifact.export", {"uri": record.uri(), "dest_dir": str(dest)})
        )
    )
    assert result["path"] == str(dest / "plan.md")
    assert Path(result["path"]).read_bytes() == b"hello artifact"
    assert result["artifact"]["uri"] == record.uri()

    # A second export never overwrites: deterministic numeric suffix.
    again = _ok(
        server.handle_line(
            _req("x2", "artifact.export", {"uri": record.uri(), "dest_dir": str(dest)})
        )
    )
    assert again["path"] == str(dest / "plan-1.md")
    assert Path(again["path"]).read_bytes() == b"hello artifact"


def test_artifact_export_rejects_bad_params(services, server, tmp_path) -> None:
    session_id = _make_session(server, "y", tmp_path)
    record = services.artifacts.create_text(session_id, "notes", "a.md", "x")
    dest = tmp_path / "out"
    dest.mkdir()
    as_file = tmp_path / "afile"
    as_file.write_text("nope")

    assert (
        _err(server.handle_line(_req("y1", "artifact.export", {"dest_dir": str(dest)})))["code"]
        == "INVALID_PARAMS"
    )
    assert (
        _err(
            server.handle_line(
                _req("y2", "artifact.export", {"uri": "artifact://nope/n/x", "dest_dir": str(dest)})
            )
        )["code"]
        == "NOT_FOUND"
    )
    assert (
        _err(server.handle_line(_req("y3", "artifact.export", {"uri": record.uri()})))["code"]
        == "INVALID_PARAMS"
    )
    assert (
        _err(
            server.handle_line(
                _req("y4", "artifact.export", {"uri": record.uri(), "dest_dir": str(as_file)})
            )
        )["code"]
        == "INVALID_PARAMS"
    )
    assert (
        _err(
            server.handle_line(
                _req(
                    "y5",
                    "artifact.export",
                    {"uri": record.uri(), "dest_dir": str(tmp_path / "missing")},
                )
            )
        )["code"]
        == "INVALID_PARAMS"
    )


def test_artifact_export_neutralizes_traversal_names(services, server, tmp_path) -> None:
    # First guard lives in the store: hostile names cannot be stored or
    # addressed at all (segment validation rejects them at write time).
    session_id = _make_session(server, "z", tmp_path)
    with pytest.raises(ArtifactURIError):
        services.artifacts.create_text(session_id, "notes", "..\\evil.md", "x")

    # Second guard lives in the handler: whatever name a record carries,
    # the export lands as a basename inside dest_dir, never outside it.
    record = services.artifacts.create_text(session_id, "notes", "ok.md", "x")
    dest = tmp_path / "out"
    dest.mkdir()
    result = _ok(
        server.handle_line(
            _req("z1", "artifact.export", {"uri": record.uri(), "dest_dir": str(dest)})
        )
    )
    exported = Path(result["path"])
    assert exported.parent == dest
    assert exported.read_bytes() == b"x"
    assert not (tmp_path / "evil.md").exists()
