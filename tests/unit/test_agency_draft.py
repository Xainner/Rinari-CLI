"""«Créalo con Rinari»: a sentence becomes a draft of a card, never an agent."""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass

import pytest

from rinari.agency.draft import parse_draft
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelResponse, StopReason

_IDS = itertools.count()


@pytest.fixture
def server(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    engine = EngineServer(build_services(app_ctx, user_home=user_home), user_home=user_home)
    yield engine
    engine.close()


def _call(server, method, params=None):
    line = {"id": f"dr-{next(_IDS)}", "method": method, "params": params or {}}
    return server.handle_line(json.dumps(line))


def _event(server, names, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("event") in names:
                return evt
        time.sleep(0.02)
    raise AssertionError(f"no {names}")


@dataclass
class _Caller:
    answer: str
    model_id: str = "fake"

    def __post_init__(self) -> None:
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResponse(content=self.answer, stop_reason=StopReason.END_TURN)


def test_a_draft_keeps_only_the_cards_text() -> None:
    answer = (
        "Claro:\n```json\n"
        '{"name": "Emails", "role": "Lee el correo", "instructions": "Avisa lo importante.",'
        ' "permission_profile": "full-access", "connectors": ["gmail"],'
        ' "budget": {"daily_usd": 99}}'
        "\n```"
    )
    assert parse_draft(answer) == {
        "name": "Emails",
        "role": "Lee el correo",
        "instructions": "Avisa lo importante.",
        "permission_profile": "full-access",
    }
    assert parse_draft('{"name": "x" , "permission_profile": "root"}')["permission_profile"] == (
        "workspace"
    )
    assert parse_draft('{"name": "  "}') is None
    assert parse_draft("no JSON here") is None
    assert parse_draft("[1, 2]") is None
    assert len(parse_draft('{"name": "' + "n" * 200 + '"}')["name"]) == 60


def test_the_draft_arrives_as_an_event_and_creates_nothing(server, monkeypatch) -> None:
    caller = _Caller(
        '{"name": "Centinela", "role": "Vigila el CI", "instructions": "Cada mañana."}'
    )
    monkeypatch.setattr(server._agency, "_draft_caller", lambda: caller)
    started = _call(server, "agency.agent.draft", {"description": "una que revise el CI"})
    assert started["ok"] is True and started["result"]["status"] == "started"
    ready = _event(server, {"agency.draft.ready", "agency.draft.failed"})
    assert ready["event"] == "agency.draft.ready"
    assert ready["payload"]["job_id"] == started["result"]["job_id"]
    assert ready["payload"]["draft"]["name"] == "Centinela"
    # The owner's sentence is data for the card, sent as the user's message.
    [request] = caller.requests
    assert request.messages[-1].content == "una que revise el CI"
    assert server._services.agency.list(None) == []


def test_a_useless_answer_is_said_plainly(server, monkeypatch) -> None:
    monkeypatch.setattr(server._agency, "_draft_caller", lambda: _Caller("No sé."))
    _call(server, "agency.agent.draft", {"description": "algo"})
    failed = _event(server, {"agency.draft.ready", "agency.draft.failed"})
    assert failed["event"] == "agency.draft.failed"
    assert "fill in the form" in failed["payload"]["message"]


def test_a_draft_needs_a_description_and_a_model(server, monkeypatch) -> None:
    assert _call(server, "agency.agent.draft", {"description": " "})["ok"] is False
    assert _call(server, "agency.agent.draft", {"description": "x" * 1001})["ok"] is False
    monkeypatch.setattr(server._agency, "_draft_caller", lambda: None)
    refused = _call(server, "agency.agent.draft", {"description": "una de correo"})
    assert refused["ok"] is False and "provider" in refused["error"]["message"]
