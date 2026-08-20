"""OpenAPI runtime tests (phase 5).

Deterministic: JSON specs on disk, httpx.MockTransport for invocations,
secrets only via env:// (monkeypatched process env), NetworkGuard for the
denial path. No sockets.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rinari.application.services import build_services
from rinari.openapi.service import ApiCallError
from rinari.openapi.spec import ApiSpecError, load_spec_file
from rinari.openapi.tools import spec_tool_definitions
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.tools.definition import ToolContext

PETSTORE = {
    "openapi": "3.0.0",
    "info": {"title": "Pets", "version": "1.0.0"},
    "servers": [{"url": "https://petstore.example/api"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            },
            "post": {
                "operationId": "createPet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                        }
                    }
                },
            },
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            },
            "delete": {"operationId": "deletePet"},
        },
    },
}

AUTHED = {
    "openapi": "3.0.1",
    "info": {"title": "Authed"},
    "servers": [{"url": "https://api.example"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "keyAuth": {"type": "apiKey", "name": "X-Api-Key", "in": "header"},
        }
    },
    "security": [{"bearerAuth": []}],
    "paths": {"/whoami": {"get": {"operationId": "whoami"}}},
}


def write_spec(tmp_path: Path, data: dict, name: str = "spec.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Spec loading / validation
# ---------------------------------------------------------------------------


def test_load_spec_file_ok(tmp_path):
    path = write_spec(tmp_path, PETSTORE)
    doc = load_spec_file(path)
    assert doc.title == "Pets"
    assert set(doc.operation_keys()) == {"listPets", "createPet", "getPet", "deletePet"}


def test_load_spec_missing_file(tmp_path):
    with pytest.raises(ApiSpecError) as excinfo:
        load_spec_file(tmp_path / "nope.json")
    assert excinfo.value.code == "SPEC_NOT_FOUND"


def test_load_spec_yaml_rejected(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("openapi: 3.0.0", encoding="utf-8")
    with pytest.raises(ApiSpecError) as excinfo:
        load_spec_file(path)
    assert excinfo.value.code == "FORMAT_UNSUPPORTED"


def test_validate_spec_rejects_bad_versions(tmp_path):
    for bad in ({"openapi": "2.0", "paths": {}}, {"openapi": "3.0.0"}, {}):
        path = write_spec(tmp_path, bad)
        with pytest.raises(ApiSpecError) as excinfo:
            load_spec_file(path)
        assert excinfo.value.code in ("SPEC_INVALID", "NO_OPERATIONS")


def test_operation_without_id_gets_generated(tmp_path):
    data = dict(PETSTORE)
    data["paths"] = {"/x": {"get": {"parameters": []}}}
    doc = load_spec_file(write_spec(tmp_path, data))
    assert doc.operations[0].operation_id == "get_x"


# ---------------------------------------------------------------------------
# Tool generation
# ---------------------------------------------------------------------------


def test_namespacing_and_risks(tmp_path):
    doc = load_spec_file(write_spec(tmp_path, PETSTORE))
    defs = spec_tool_definitions("pets", doc)
    by = {d.name: d for d in defs}
    assert by["api.pets.listPets"].risk == "low"
    assert by["api.pets.listPets"].idempotent is True
    assert by["api.pets.createPet"].risk == "medium"
    assert by["api.pets.deletePet"].risk == "high"
    assert by["api.pets.deletePet"].side_effects == "remote-destructive"


def test_override_changes_risk(tmp_path):
    doc = load_spec_file(write_spec(tmp_path, PETSTORE))
    defs = spec_tool_definitions(
        "pets",
        doc,
        overrides={"deletePet": {"risk": "medium", "side_effects": "remote-reversible"}},
    )
    d = next(x for x in defs if x.name.endswith("deletePet"))
    assert d.risk == "medium"
    assert d.side_effects == "remote-reversible"


def test_path_param_required_in_schema(tmp_path):
    doc = load_spec_file(write_spec(tmp_path, PETSTORE))
    defs = spec_tool_definitions("pets", doc)
    get = next(x for x in defs if x.name.endswith("getPet"))
    assert get.input_schema["required"] == ["petId"]


def test_auth_requirements_detected(tmp_path):
    doc = load_spec_file(write_spec(tmp_path, AUTHED))
    defs = spec_tool_definitions("authed", doc)
    manifest = defs[0].manifest
    assert manifest["auth"][0] == {"scheme": "bearerAuth", "kind": "bearer"}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.fixture
def services(app_ctx):
    return build_services(app_ctx)


def test_add_list_show(services, tmp_path):
    services.api.add("pets", write_spec(tmp_path, PETSTORE), auth={"bearerAuth": "env://PET_KEY"})
    rows = services.api.list()
    assert rows[0]["name"] == "pets"
    shown = services.api.show("pets")
    assert shown["auth"] == {"bearerAuth": "env://PET_KEY"}


def test_add_rejects_plain_secret(services, tmp_path):
    with pytest.raises(ApiSpecError) as excinfo:
        services.api.add("pets", write_spec(tmp_path, PETSTORE), auth={"bearerAuth": "raw-token"})
    assert excinfo.value.code == "SPEC_INVALID"


def test_validate_ok_and_drift(services, tmp_path):
    spec = write_spec(tmp_path, PETSTORE)
    services.api.add("pets", spec)
    assert services.api.validate("pets")["ok"] is True
    # Drift: spec file no longer valid.
    spec.write_text(json.dumps({"openapi": "nope"}), encoding="utf-8")
    data = services.api.validate("pets")
    assert data["ok"] is False


def test_disable_hides_tools(services, tmp_path):
    services.api.add("pets", write_spec(tmp_path, PETSTORE))
    assert services.api.tool_definitions("pets")
    services.api.disable("pets")
    assert services.api.tool_definitions("pets") == []


def test_remove(services, tmp_path):
    services.api.add("pets", write_spec(tmp_path, PETSTORE))
    assert services.api.remove("pets") is True
    assert services.api.list() == []


# ---------------------------------------------------------------------------
# Invocation (MockTransport)
# ---------------------------------------------------------------------------


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def make_ctx(tmp_path, *, client=None, network=None) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    from rinari.policy.engine import PermissionProfile
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.runtime.cancellation import CancellationToken
    from rinari.shared.clock import FakeClock

    return ToolContext(
        session_id="ses-api",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        web=(lambda: client) if client is not None else None,
        network=network,
    )


def test_invoke_builds_url_and_auth(services, tmp_path, monkeypatch):
    services.api.add("authed", write_spec(tmp_path, AUTHED), auth={"bearerAuth": "env://PET_KEY"})
    monkeypatch.setenv("PET_KEY", "sekrit")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"me": "rinari"})

    doc = load_spec_file(write_spec(tmp_path, AUTHED))
    op = next(o for o in doc.operations if o.operation_id == "whoami")
    payload = services.api.invoke("authed", op, {}, _env(_mock_client(handler)))
    assert payload["ok"] is True
    assert payload["body"] == {"me": "rinari"}
    assert seen["auth"] == "Bearer sekrit"
    assert seen["url"].startswith("https://api.example/whoami")


def test_invoke_missing_secret_is_auth_required(services, tmp_path, monkeypatch):
    services.api.add(
        "authed", write_spec(tmp_path, AUTHED), auth={"bearerAuth": "env://MISSING_VAR"}
    )
    monkeypatch.delenv("MISSING_VAR", raising=False)
    doc = load_spec_file(write_spec(tmp_path, AUTHED))
    op = next(o for o in doc.operations if o.operation_id == "whoami")
    with pytest.raises(ApiCallError) as excinfo:
        services.api.invoke("authed", op, {}, _env(_mock_client(lambda r: httpx.Response(200))))
    assert excinfo.value.code == "AUTH_REQUIRED"


def test_invoke_network_guard_denied(services, tmp_path):
    services.api.add("pets", write_spec(tmp_path, PETSTORE))
    guard = NetworkGuard(NetworkPolicy(mode="off"))
    doc = load_spec_file(write_spec(tmp_path, PETSTORE))
    op = next(o for o in doc.operations if o.operation_id == "listPets")
    with pytest.raises(ApiCallError) as excinfo:
        services.api.invoke(
            "pets", op, {}, _env(_mock_client(lambda r: httpx.Response(200)), network=guard)
        )
    assert excinfo.value.code == "NETWORK_ERROR"


def test_invoke_path_param_substitution(services, tmp_path):
    services.api.add("pets", write_spec(tmp_path, PETSTORE))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "7"})

    doc = load_spec_file(write_spec(tmp_path, PETSTORE))
    op = next(o for o in doc.operations if o.operation_id == "getPet")
    payload = services.api.invoke("pets", op, {"petId": "7"}, _env(_mock_client(handler)))
    assert payload["ok"] is True
    assert seen["url"] == "https://petstore.example/api/pets/7"


def test_handler_returns_toolresult_with_error(tmp_path, services):
    services.api.add("pets", write_spec(tmp_path, PETSTORE))
    defs = services.api.tool_definitions("pets")
    get = next(d for d in defs if d.name.endswith("getPet"))
    ctx = make_ctx(tmp_path, client=_mock_client(lambda r: httpx.Response(500, text="boom")))
    result = get.handler({"petId": "1"}, ctx)
    assert result.ok is False


def _env(client, network=None):
    from rinari.openapi.service import _InvocationEnv

    return _InvocationEnv(client=client, network_guard=network, timeout_s=5.0)
