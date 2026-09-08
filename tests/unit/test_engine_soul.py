"""Engine Protocol slice 8a: Soul 3.0 definitions, store, activation."""

from __future__ import annotations

import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.runtime.identity import load_active_soul


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


def test_list_contains_bundled_default(server) -> None:
    result = _ok(server.handle_line(_req("s1", "soul.list", {})))
    by_id = {s["id"]: s for s in result["souls"]}
    assert "rinari-default" in by_id
    default = by_id["rinari-default"]
    assert default["source"] == "bundled"
    assert default["version"] == "3.0"
    assert result["active_id"] is None
    # List view carries no identity text.
    assert "identity" not in default


def test_create_get_update_activate_remove(server) -> None:
    created = _ok(
        server.handle_line(
            _req(
                "s2",
                "soul.create",
                {"id": "mio", "name": "Mio", "identity": "You are Mio, a test soul."},
            )
        )
    )
    assert created["soul"]["source"] == "custom"
    assert "test soul" in created["soul"]["identity"]

    fetched = _ok(server.handle_line(_req("s3", "soul.get", {"id": "mio"})))
    assert fetched["soul"]["name"] == "Mio"

    updated = _ok(
        server.handle_line(_req("s4", "soul.update", {"id": "mio", "description": "edited"}))
    )
    assert updated["soul"]["description"] == "edited"

    activated = _ok(server.handle_line(_req("s5", "soul.activate", {"id": "mio"})))
    assert activated["soul"]["id"] == "mio"

    listed = _ok(server.handle_line(_req("s6", "soul.list", {})))
    assert listed["active_id"] == "mio"

    removed = _ok(server.handle_line(_req("s7", "soul.remove", {"id": "mio"})))
    assert removed["removed"]["id"] == "mio"
    assert _err(server.handle_line(_req("s8", "soul.get", {"id": "mio"})))["code"] == "NOT_FOUND"
    # Removing the active soul clears the pointer.
    assert _ok(server.handle_line(_req("s9", "soul.list", {})))["active_id"] is None


def test_bundled_soul_is_read_only(server) -> None:
    assert (
        _err(server.handle_line(_req("s10", "soul.update", {"id": "rinari-default"})))["code"]
        == "INVALID_USAGE"
    )
    assert (
        _err(server.handle_line(_req("s11", "soul.remove", {"id": "rinari-default"})))["code"]
        == "INVALID_USAGE"
    )


def test_create_validates(server) -> None:
    assert (
        _err(
            server.handle_line(
                _req(
                    "s12",
                    "soul.create",
                    {"id": "Bad ID!", "name": "x", "identity": "y"},
                )
            )
        )["code"]
        == "INVALID_USAGE"
    )
    assert (
        _err(
            server.handle_line(
                _req("s13", "soul.create", {"id": "empty", "name": "x", "identity": "  "})
            )
        )["code"]
        == "INVALID_USAGE"
    )
    _ok(server.handle_line(_req("s14", "soul.create", {"id": "dup", "name": "x", "identity": "y"})))
    assert (
        _err(
            server.handle_line(
                _req("s15", "soul.create", {"id": "dup", "name": "x", "identity": "y"})
            )
        )["code"]
        == "CONFLICT"
    )


def test_active_resolution_order(services, tmp_path) -> None:
    home = services.ctx.home
    # 1. Bundled default when nothing else exists.
    asset = load_active_soul(home)
    assert asset.name == "soul:rinari-default"
    assert asset.text.startswith("You are Rinari,")
    # 2. Legacy ~/soul.md wins over the bundled default, untouched.
    (home / "soul.md").write_text("Legacy soul text.", encoding="utf-8")
    asset = load_active_soul(home)
    assert asset.text == "Legacy soul text."
    # 3. Explicit activation wins over legacy.
    store_home = home
    from rinari.soul.store import SoulStore

    SoulStore(store_home).create("alt", name="Alt", identity="Alt soul text.")
    SoulStore(store_home).activate("alt")
    asset = load_active_soul(home)
    assert asset.text == "Alt soul text."
    assert (home / "soul.md").read_text(encoding="utf-8") == "Legacy soul text."
