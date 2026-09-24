"""Slash commands from one catalog: what each client lists, how the Engine
expands `mode`, `turn` and `skill` commands, and the terminal's /skill."""

from __future__ import annotations

import itertools
import json
import time

import pytest
from rich.console import Console

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime, slash
from rinari.cli.agent_runtime import build_agent_session
from rinari.commands import CommandError, command_list, expand_command
from rinari.commands.catalog import REVIEW_PROMPT, TEST_PROMPT
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.shared.errors import InvalidUsageError


class FakeModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content="ok", stop_reason=StopReason.END_TURN)


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    container = build_services(app_ctx, user_home=home)
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


# -- catalog ------------------------------------------------------------------------


def test_each_client_lists_its_commands_and_skills_join_them(services) -> None:
    desktop = {row["name"]: row for row in command_list(services.skills, client="desktop")}
    assert {"plan", "build", "review", "test", "skill", "new", "compact", "fork"} <= set(desktop)
    assert "exit" not in desktop and "attach" not in desktop
    # Enabled skills become commands; a built-in keeps its name ("test").
    assert desktop["research"]["kind"] == "skill" and desktop["research"]["source"] == "skill"
    assert desktop["test"]["source"] == "builtin"
    services.skills.set_enabled("research", False)
    assert "research" not in {row["name"] for row in command_list(services.skills)}
    cli = {row["name"] for row in command_list(client="cli")}
    assert {"exit", "attach", "tasks"} <= cli and "fork" not in cli


def test_the_engine_expands_modes_turns_and_skills(services) -> None:
    review = expand_command("review", "", services.skills)
    assert review.mode == "review" and review.message == REVIEW_PROMPT
    assert expand_command("review", "solo el parser").message == "solo el parser"
    with pytest.raises(CommandError) as empty_plan:
        expand_command("plan", "")
    assert empty_plan.value.code == "TEXT_REQUIRED"
    test = expand_command("test", "solo la unidad de auth")
    assert test.message == f"{TEST_PROMPT}\n\nsolo la unidad de auth" and test.mode is None
    skill = expand_command("research", "precios de GPUs", services.skills)
    assert skill.skill == "research" and skill.message == "precios de GPUs"
    by_name = expand_command("skill", "research", services.skills)
    assert by_name.skill == "research" and "research" in by_name.message
    for name, code in (("new", "CLIENT_COMMAND"), ("nope", "UNKNOWN_COMMAND")):
        with pytest.raises(CommandError) as exc:
            expand_command(name, "", services.skills)
        assert exc.value.code == code
    with pytest.raises(CommandError) as missing:
        expand_command("skill", "does-not-exist", services.skills)
    assert missing.value.code == "SKILL_NOT_FOUND"


# -- protocol ------------------------------------------------------------------------

_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"cmd-{next(_IDS)}", "method": method, "params": params or {}}
    return server.handle_line(json.dumps(line))


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _events_until_done(server, session_id, timeout=20.0):
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for frame in server.drain_events():
            if frame.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(frame)
            if frame.get("event") in ("turn.completed", "turn.failed", "turn.cancelled"):
                return seen
        time.sleep(0.02)
    return seen


def test_a_command_turn_switches_mode_and_the_model_gets_the_prompt(
    services, tmp_path, monkeypatch
) -> None:
    fake = FakeModel()
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    server = EngineServer(services, user_home=tmp_path / "home")
    try:
        session_id = _ok(_call(server, "session.create", {"cwd": str(tmp_path), "chat": True}))[
            "session"
        ]["id"]
        listed = {row["name"] for row in _ok(_call(server, "command.list", {}))["commands"]}
        assert {"review", "research"} <= listed

        _ok(
            _call(
                server,
                "session.turn.start",
                {"session_id": session_id, "message": "/review", "command": {"name": "review"}},
            )
        )
        events = _events_until_done(server, session_id)
        assert any(frame["event"] == "session.mode.changed" for frame in events)
        assert services.sessions.show(session_id).mode == "review"
        sent = "\n".join(str(message.content) for message in fake.requests[-1].messages)
        assert "Review the uncommitted changes" in sent
        history = services.ctx.message_repo.list(session_id)
        assert any(m.role == "user" and m.display_content == "/review" for m in history)

        _ok(
            _call(
                server,
                "session.turn.start",
                {
                    "session_id": session_id,
                    "message": "/research precios",
                    "command": {"name": "research", "text": "precios"},
                },
            )
        )
        _events_until_done(server, session_id)
        pinned = dict(services.sessions.show(session_id).active_skills or ())
        assert "research" in pinned

        bad = _call(
            server,
            "session.turn.start",
            {"session_id": session_id, "message": "/new", "command": {"name": "new"}},
        )
        assert bad["ok"] is False and bad["error"]["details"]["command_code"] == "CLIENT_COMMAND"
    finally:
        server.close()


# -- terminal ------------------------------------------------------------------------


def test_skill_commands_in_the_terminal(services, tmp_path, monkeypatch) -> None:
    fake = FakeModel()
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    record = services.sessions.start(tmp_path, forced_chat=True).session
    session = build_agent_session(services, record, interactive=False, user_home=tmp_path / "home")
    try:
        console = Console(record=True, no_color=True, width=120)
        out = slash.handle(session, console, "/research precios de GPUs")
        assert out.action == "turn" and out.prompt == "precios de GPUs"
        assert "research" in dict(session.record.active_skills or ())
        out = slash.handle(session, console, "/skill debug")
        assert out.action == "turn" and "debug" in out.prompt
        with pytest.raises(InvalidUsageError):
            slash.handle(session, console, "/nope")
        slash.handle(session, console, "/help")
        help_text = console.export_text()
        assert "/plan [text]" in help_text and "/<skill> [text]" in help_text
    finally:
        session.end()
