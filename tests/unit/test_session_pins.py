"""Pinned conversations (desktop «Fijados»): `session.pin`, `pinned_at` and
`rinari session pin|unpin`. A pin is organizational state owned by the Engine,
so the CLI and the desktop see the same list."""

from __future__ import annotations

import itertools
import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.main import app
from rinari.engine_protocol.server import EngineServer
from rinari.shared.paths import ENV_HOME


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


_IDS = itertools.count()


def _call(server, method, params):
    line = json.dumps({"id": f"pin-{next(_IDS)}", "method": method, "params": params})
    return server.handle_line(line)


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _chat(server, tmp_path):
    return _ok(_call(server, "session.create", {"cwd": str(tmp_path), "chat": True}))["session"]


def test_pin_and_unpin_over_the_protocol(server, tmp_path) -> None:
    created = _chat(server, tmp_path)
    assert created["pinned_at"] is None

    pinned = _ok(_call(server, "session.pin", {"ref": created["id"], "pinned": True}))["session"]
    assert pinned["pinned_at"]
    listed = _ok(_call(server, "session.list", {}))["sessions"]
    assert next(s for s in listed if s["id"] == created["id"])["pinned_at"] == pinned["pinned_at"]

    # Idempotent: pinning again keeps the original moment (the list order).
    again = _ok(_call(server, "session.pin", {"ref": created["id"], "pinned": True}))["session"]
    assert again["pinned_at"] == pinned["pinned_at"]

    unpinned = _ok(_call(server, "session.pin", {"ref": created["id"], "pinned": False}))
    assert unpinned["session"]["pinned_at"] is None


def test_pin_rejects_bad_params_and_unknown_sessions(server, tmp_path) -> None:
    created = _chat(server, tmp_path)
    bad = _call(server, "session.pin", {"ref": created["id"], "pinned": "yes"})
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"
    missing = _call(server, "session.pin", {"ref": "ses_missing", "pinned": True})
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"


def test_a_stale_record_saved_by_a_turn_cannot_unpin(services, tmp_path) -> None:
    started = services.sessions.start(tmp_path, forced_chat=True).session
    stale = services.sessions.show(started.id)  # what a running turn holds
    services.sessions.set_pinned(started.id, True)
    services.ctx.session_repo.update(replace(stale, title="renamed by the turn"))
    after = services.sessions.show(started.id)
    assert after.title == "renamed by the turn"
    assert after.pinned_at is not None


def test_pinning_does_not_reorder_activity_and_survives_archive(services, tmp_path) -> None:
    started = services.sessions.start(tmp_path, forced_chat=True).session
    before = services.sessions.show(started.id).last_active_at
    services.sessions.set_pinned(started.id, True)
    assert services.sessions.show(started.id).last_active_at == before

    services.sessions.archive(started.id)
    services.sessions.restore(started.id)
    assert services.sessions.show(started.id).pinned_at is not None
    events = [e.type for e in services.ctx.event_repo.list(started.id)]
    assert "SessionPinned" in events


def test_cli_pin_unpin_and_list_marker(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.setenv("RINARI_KEYRING", "0")
    monkeypatch.chdir(work)

    def call(*args):
        return runner.invoke(app, list(args), catch_exceptions=False)

    for args in (
        ("providers", "add", "openai", "--name", "fake", "--api-key", "sk-fake-test"),
        ("models", "add", "--provider", "fake", "--model", "fake-1", "--name", "f1"),
        ("provider", "use", "fake"),
    ):
        assert call(*args).exit_code == 0
    created = json.loads(call("--json", "session", "new", "--chat", "--name", "alpha").output)
    session_id = created["data"]["id"]

    pinned = call("--json", "session", "pin", session_id)
    assert pinned.exit_code == 0, pinned.output
    assert json.loads(pinned.output)["data"]["pinned_at"]
    listed = call("session", "list").output  # the table may wrap the title
    assert "[pin]" in listed and "alpha" in listed

    unpinned = call("--json", "session", "unpin", session_id)
    assert json.loads(unpinned.output)["data"]["pinned_at"] is None
    assert "[pin]" not in call("session", "list").output
