"""Fallback and auxiliary models (plan 0.4.0 #80)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.providers.errors import ProviderError, ProviderErrorCode
from rinari.runtime.agent import AgentLoop
from rinari.runtime.model_caller import SessionModelGateway
from tests.unit import test_agent_loop as base

env = base.env


@dataclass
class Scripted:
    model_id: str
    scripted: list
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _answer(text: str) -> ModelResponse:
    return ModelResponse(content=text, stop_reason=StopReason.END_TURN)


def _error(code: ProviderErrorCode) -> ProviderError:
    return ProviderError(f"provider failed: {code}", code=code, retryable=False)


def _loop(env, primary, fallback):
    gateway = SessionModelGateway(primary)
    gateway.fallback_factory = lambda: fallback
    activity: list = []
    loop = AgentLoop(
        gateway,
        env["runtime"],
        env["assembler"],
        activity_sink=lambda n, p: activity.append((n, p)),
    )
    return loop, activity, gateway


def test_a_technical_failure_goes_on_with_the_fallback_and_says_so(env) -> None:
    primary = Scripted("main", [_error(ProviderErrorCode.SERVER_ERROR)])
    fallback = Scripted("backup", [_answer("Seguí con el respaldo.")])
    loop, activity, gateway = _loop(env, primary, fallback)
    result = loop.turn(env["ctx"], "hola")
    assert result.content == "Seguí con el respaldo."
    assert fallback.requests[0].model == "backup"
    notice = next(p for n, p in activity if n == "model.fallback")
    assert notice["to_model"] == "backup" and notice["reason"] == "SERVER_ERROR"

    # The next turn tries the conversation's own model again.
    primary.scripted.append(_answer("De vuelta al principal."))
    assert loop.turn(env["ctx"], "otra").content == "De vuelta al principal."
    assert not gateway.on_fallback


def test_out_of_quota_switches_at_once(env) -> None:
    primary = Scripted("main", [_error(ProviderErrorCode.QUOTA_EXHAUSTED)])
    fallback = Scripted("backup", [_answer("ok")])
    loop, _activity, _ = _loop(env, primary, fallback)
    assert loop.turn(env["ctx"], "hola").content == "ok"
    assert len(primary.requests) == 1


def test_a_content_refusal_never_uses_the_fallback(env) -> None:
    primary = Scripted("main", [_error(ProviderErrorCode.SAFETY_BLOCK)])
    fallback = Scripted("backup", [_answer("no debería")])
    loop, activity, _ = _loop(env, primary, fallback)
    with pytest.raises(ProviderError):
        loop.turn(env["ctx"], "hola")
    assert fallback.requests == []
    assert not any(n == "model.fallback" for n, _ in activity)


def test_two_empty_answers_switch_to_the_fallback(env) -> None:
    primary = Scripted("main", [_answer(""), _answer("")])
    fallback = Scripted("backup", [_answer("Aquí estoy.")])
    loop, activity, _ = _loop(env, primary, fallback)
    result = loop.turn(env["ctx"], "hola")
    assert result.kind == "answer" and result.content == "Aquí estoy."
    assert any(p["reason"] == "empty_response" for n, p in activity if n == "model.fallback")


def test_without_a_fallback_the_failure_is_the_turns(env) -> None:
    primary = Scripted("main", [_error(ProviderErrorCode.SERVER_ERROR)])
    gateway = SessionModelGateway(primary)
    loop = AgentLoop(gateway, env["runtime"], env["assembler"])
    with pytest.raises(ProviderError):
        loop.turn(env["ctx"], "hola")


# -- the settings ----------------------------------------------------------------------


def test_roles_are_set_by_alias_and_cleared(app_ctx, tmp_path) -> None:
    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.models.roles import get_roles, set_roles

    home = tmp_path / "home"
    home.mkdir()
    services = build_services(app_ctx, user_home=home)
    services.providers.add(
        AddProviderInput(alias="fake", provider_type="openai", secret="dummy-not-real")
    )
    model = services.models.add("fake", "cheap-model", "cheap")
    assert get_roles(app_ctx) == {
        "fallback": None,
        "auxiliary": None,
        "auxiliary_compaction": False,
    }
    roles = set_roles(app_ctx, services.models, auxiliary="cheap", auxiliary_compaction=True)
    assert roles["auxiliary"] == model.id and roles["auxiliary_compaction"] is True
    assert set_roles(app_ctx, services.models, auxiliary=None)["auxiliary"] is None

    from rinari.cli.agent_runtime import _auxiliary_caller

    sentinel = object()
    assert _auxiliary_caller(services, sentinel) is sentinel
    set_roles(app_ctx, services.models, auxiliary="cheap")
    assert _auxiliary_caller(services, sentinel).model_id == model.id
