"""A failed turn persists why it failed, not only the exception's class name."""

from __future__ import annotations

import json
import time

import pytest

from rinari.application.introspection import Introspection
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import _interruption_detail
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ProviderCapabilities
from rinari.providers.errors import ProviderError, ProviderErrorCode


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


def _limit() -> ProviderError:
    error = ProviderError(
        "Provider returned HTTP 429 for https://api.example/v1: slow down "
        "Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz0123456789",
        code=ProviderErrorCode.RATE_LIMIT,
        retryable=True,
    )
    error.details["http_status"] = 429
    return error


def test_the_detail_carries_code_status_and_a_redacted_bounded_message() -> None:
    detail = _interruption_detail(_limit())
    assert detail["code"] == "RATE_LIMIT"
    assert detail["http_status"] == 429
    assert detail["retryable"] is True
    assert "slow down" in detail["message"]
    assert "sk-live-abcdefghijklmnopqrstuvwxyz0123456789" not in detail["message"]
    long = _interruption_detail(RuntimeError("x" * 5000))
    assert len(long["message"]) <= 500
    assert "code" not in long or long["code"]


class _Failing:
    def capabilities(self):
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request):
        raise _limit()


def test_a_provider_failure_is_persisted_with_its_code_and_message(
    services, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_: _Failing())
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        created = engine.handle_line(
            json.dumps(
                {
                    "id": "c",
                    "method": "session.create",
                    "params": {"cwd": str(tmp_path), "chat": True},
                }
            )
        )
        session_id = created["result"]["session"]["id"]
        engine.handle_line(
            json.dumps(
                {
                    "id": "s",
                    "method": "session.turn.start",
                    "params": {"session_id": session_id, "message": "hola"},
                }
            )
        )
        deadline = time.time() + 20
        while engine.has_active_turns() and time.time() < deadline:
            time.sleep(0.02)
        row = services.ctx.event_repo.latest(session_id, ["TurnInterrupted"])
        assert row is not None
        assert row.payload["reason"] == "ProviderError"
        assert row.payload["code"] == "RATE_LIMIT"
        assert row.payload["http_status"] == 429
        assert "slow down" in row.payload["message"]
        # The introspection reads it as a failure with its cause.
        sessions = Introspection(services).session(session_id)
        assert sessions["turns"][-1]["outcome"] == "failed"
    finally:
        engine.close()
