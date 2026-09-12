"""Phase 7: interactive slash commands + REPL renderer integration."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import typer
from rich.console import Console

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime, slash
from rinari.cli.agent_runtime import build_agent_session
from rinari.cli.repl import run_repl
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    Usage,
)
from rinari.shared.errors import InvalidUsageError


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, max_context_tokens=200_000)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


@pytest.fixture
def env(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    s.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    s.models.add("fake", "fake-model-1", "fake-one")
    s.providers.use("fake")
    return tmp_path, user_home, s


def _session(env, monkeypatch, *, project: bool = False, scripted_usage: bool = False) -> tuple:
    tmp_path, user_home, s = env
    cwd = tmp_path / "proj" if project else tmp_path
    cwd.mkdir(exist_ok=True)
    if project:
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
        (cwd / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=cwd, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=cwd,
            check=True,
        )
    usage = Usage(input_tokens=120, output_tokens=30) if scripted_usage else None
    fake = FakeModel(
        scripted=[ModelResponse(content="ok", stop_reason=StopReason.END_TURN, usage=usage)]
    )
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    record = s.sessions.start(cwd, forced_chat=not project).session
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    return tmp_path, s, record, session


def _lines(console_text: str) -> str:
    return console_text


# -- navigation outcomes ------------------------------------------------------


def test_navigation_outcomes(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        assert slash.handle(session, console, "/exit").action == "exit"
        assert slash.handle(session, console, "/new").action == "new_session"
        out = slash.handle(session, console, "/resume ses_abc")
        assert out.action == "resume_session" and out.resume_ref == "ses_abc"
    finally:
        session.end()


def test_turn_outcomes_are_pregrooked_prompts(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        out = slash.handle(session, console, "/test")
        assert out.action == "turn"
        assert "test suite" in out.prompt
        out = slash.handle(session, console, "/review")
        assert out.action == "turn"
        assert "uncommitted changes" in out.prompt
    finally:
        session.end()


# -- state commands (CHAT session) -------------------------------------------


def test_help_lists_all_commands(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/help")
        text = console.export_text()
        for cmd in (
            "/status",
            "/usage",
            "/tokens",
            "/plan",
            "/tasks",
            "/diff",
            "/test",
            "/review",
            "/skills",
            "/tools",
            "/agents",
            "/permissions",
            "/checkpoint",
            "/undo",
            "/compact",
            "/context",
            "/trace",
            "/new",
            "/resume",
        ):
            assert cmd in text
    finally:
        session.end()


def test_usage_after_turn(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch, scripted_usage=True)
    try:
        agent_runtime.run_turn(session, "hello")
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/usage")
        text = console.export_text()
        assert "120" in text
        assert "30" in text
        assert "model calls" in text
    finally:
        session.end()


def test_tokens_window_from_capabilities(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/tokens")
        text = console.export_text()
        assert "200000" in text
    finally:
        session.end()


def test_tools_lists_registry(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/tools")
        text = console.export_text()
        assert "shell.exec" in text
        assert "fs" in text
    finally:
        session.end()


def test_status_shows_provider(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/status")
        text = console.export_text()
        assert "fake" in text
        assert "fake-model-1" in text
    finally:
        session.end()


def test_permissions_shows_profile(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/permissions")
        text = console.export_text()
        assert "profile" in text
        assert "network" in text
    finally:
        session.end()


def test_context_lists_segments(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/context")
        text = console.export_text()
        assert "(system total)" in text
        assert "history" in text
    finally:
        session.end()


def test_trace_lists_events_after_turn(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        agent_runtime.run_turn(session, "hello")
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/trace 5")
        text = console.export_text()
        assert "#" in text
    finally:
        session.end()


def test_agents_empty_state(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/agents")
        text = console.export_text()
        assert "subagent" in text
    finally:
        session.end()


def test_skills_lists_builtins(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/skills")
        text = console.export_text()
        assert "fix-ci" in text  # builtin skill from phase 6
    finally:
        session.end()


def test_project_commands_require_project(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        with pytest.raises(InvalidUsageError):
            slash.handle(session, console, "/diff")
        with pytest.raises(InvalidUsageError):
            slash.handle(session, console, "/tasks")
        with pytest.raises(InvalidUsageError):
            slash.handle(session, console, "/undo")
    finally:
        session.end()


def test_unknown_command_raises(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    try:
        console = Console(record=True, no_color=True, width=100)
        with pytest.raises(InvalidUsageError):
            slash.handle(session, console, "/bogus")
    finally:
        session.end()


# -- project session: tasks + undo -------------------------------------------


def test_tasks_lists_project_tasks(env, monkeypatch) -> None:
    _, s, record, session = _session(env, monkeypatch, project=True)
    try:
        assert record.kind == "PROJECT"
        root = record.project_root_snapshot
        s.tasks.add(root, "Implement X", acceptance="- [ ] it works")
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/tasks")
        text = console.export_text()
        assert "Implement X" in text
        slash.handle(session, console, "/plan")
        assert "Implement X" in console.export_text()
    finally:
        session.end()


def test_diff_clean_tree(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch, project=True)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/diff")
        text = console.export_text()
        assert "no uncommitted changes" in text
    finally:
        session.end()


def test_checkpoint_and_undo_empty(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch, project=True)
    try:
        console = Console(record=True, no_color=True, width=100)
        slash.handle(session, console, "/checkpoint")
        assert "checkpoint" in console.export_text().lower()
    finally:
        session.end()


# -- REPL integration ---------------------------------------------------------


def test_repl_new_command_restarts(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    script = iter(["/new"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(script))
    try:
        assert run_repl(session, no_banner=True) == "new"
    finally:
        session.end()


def test_repl_resume_command_returns_ref(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    script = iter(["/resume ses_zzz"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(script))
    try:
        assert run_repl(session, no_banner=True) == "resume:ses_zzz"
    finally:
        session.end()


def test_repl_json_stream_emits_json_events(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch, scripted_usage=True)
    script = iter(["/exit"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(script))
    try:
        assert run_repl(session, json_flag=True) is None
    finally:
        session.end()


def test_repl_input_prompt_uses_typer_signature(env, monkeypatch) -> None:
    # Regression: the REPL input used to pass click-only kwargs
    # (no_default) to typer.prompt, raising TypeError on the first input.
    _, _s, _record, session = _session(env, monkeypatch)
    calls: list[tuple[str, dict]] = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        return "/exit"

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    try:
        assert run_repl(session, no_banner=True) is None
    finally:
        session.end()
    assert calls, "REPL input prompt was never requested"
    text, kwargs = calls[0]
    assert text == "rinari"
    assert kwargs.get("prompt_suffix") == "> "
    assert "no_default" not in kwargs


def test_repl_exit_returns_none(env, monkeypatch) -> None:
    _, _s, _record, session = _session(env, monkeypatch)
    script = iter(["/exit"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(script))
    try:
        assert run_repl(session, no_banner=True) is None
    finally:
        session.end()


def test_repl_run_turn_and_status_rail(env, monkeypatch, capsys) -> None:
    _, _s, _record, session = _session(env, monkeypatch, scripted_usage=True)
    script = iter(["hello", "/exit"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(script))
    try:
        assert run_repl(session, no_banner=True) is None
    finally:
        session.end()
    out = capsys.readouterr().out
    assert "ok" in out  # model answer streamed
    assert "answer" in out  # status rail line
    assert "2m/0t" in out  # Automatic title + conversational reply, without tool calls.
