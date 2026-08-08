"""Tests del CLI: one-shot run y listado de modelos (con mock transport).

typer.testing.CliRunner ejercita los entrypoints sin TTY.
"""

import json

import httpx
import pytest
from typer.testing import CliRunner

from rinari.cli import app

runner = CliRunner()


def make_transport_ok(content: str = "respuesta"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}, {"id": "otro"}]})
        body = json.loads(request.content)
        if body.get("stream"):
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            }
            done = {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            payload = (
                f"data: {json.dumps(chunk)}\n\n"
                f"data: {json.dumps(done)}\n\n"
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def patch_llm_client(monkeypatch):
    """Fuerza un transport mock en LLMClient para todos los tests del CLI."""
    from rinari import client as client_mod

    orig_init = client_mod.LLMClient.__init__

    def fake_init(self, base_url, api_key=None, model="test-model", timeout=300.0, transport=None, provider="openai"):
        # Inyecta MockTransport SIEMPRE (ignora el real)
        orig_init(self, base_url=base_url, api_key=api_key, model=model, timeout=timeout,
                  transport=make_transport_ok())

    monkeypatch.setattr(client_mod.LLMClient, "__init__", fake_init)


@pytest.fixture(autouse=True)
def fake_home_config(monkeypatch, tmp_path):
    """Config sin key para no depender del HOME real."""
    from rinari import config as config_mod

    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("MY_API_KEY", "sk-test")


def test_run_one_shot_streams_to_stdout():
    result = runner.invoke(app, ["run", "hola"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_run_with_profile():
    result = runner.invoke(app, ["run", "hola", "--profile", "default"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_run_no_stream():
    result = runner.invoke(app, ["run", "hola", "--no-stream"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_models_lists_ids():
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "test-model" in result.stdout


def test_unknown_profile_fails_cleanly():
    result = runner.invoke(app, ["run", "hola", "--profile", "nope"])
    assert result.exit_code == 1
    assert "no existe" in result.stdout


def test_identity_shows_rinari():
    result = runner.invoke(app, ["identity"])
    assert result.exit_code == 0
    assert "Rinari" in result.stdout
    assert "Tsundere" in result.stdout


def test_version_shows_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Rinari CLI" in result.stdout


def test_update_runs_git_pull(monkeypatch):
    """update llama a git pull en el repo (mockeamos subprocess)."""
    import subprocess

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        return type("R", (), {"returncode": 0, "stdout": "Already up to date.", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert calls["cmd"][:2] == ["git", "pull"]
    assert calls["cwd"] is not None  # apunta al repo


def test_sync_runs_uv_sync(monkeypatch):
    import subprocess

    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert calls["cmd"] == ["uv", "sync"]


def test_system_prompt_has_rinari_identity():
    from rinari.identity import build_chat_prompt

    prompt = build_chat_prompt()
    assert "Rinari" in prompt
    assert "maid" in prompt.lower()
    assert "NUNCA digas" in prompt or "nunca digas" in prompt.lower()


def test_agent_command_runs_loop(monkeypatch, tmp_path):
    """agent orquesta el loop con el cwd indicado."""
    from rinari import agent as agent_mod
    from rinari.agent import loop as loop_mod

    calls = {}

    def fake_run_agent(task, client, cwd, auto_approve, max_iterations, render_callback, **kwargs):
        calls["task"] = task
        calls["cwd"] = cwd
        return {
            "status": "done",
            "final": "Listo (✿◠‿◠)",
            "steps": [{"type": "final", "content": "Listo (✿◠‿◠)"}],
            "iterations": 1,
        }

    monkeypatch.setattr(loop_mod, "run_agent", fake_run_agent)
    result = runner.invoke(app, ["agent", "crea un archivo", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert calls["task"] == "crea un archivo"
    assert "Tarea completada" in result.stdout


def test_agent_without_task_enters_interactive_mode(monkeypatch, tmp_path):
    """agent sin tarea → modo interactivo: mantiene contexto entre turnos."""
    from rinari import cli as cli_mod
    from rinari.agent import loop as loop_mod

    inputs = iter(["primera tarea", "segunda tarea", "/exit"])
    calls = []

    def fake_run_agent(task, client, cwd, auto_approve, max_iterations, render_callback, messages=None, **kwargs):
        calls.append({"task": task, "messages": messages})
        prev = list(messages) if messages else []
        return {
            "status": "done",
            "final": f"respuesta a {task}",
            "steps": [],
            "iterations": 1,
            "messages": prev
            + [
                {"role": "user", "content": f"Tarea: {task}"},
                {"role": "assistant", "content": f"respuesta a {task}"},
            ],
        }

    monkeypatch.setattr(loop_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod.console, "input", lambda prompt="": next(inputs))

    result = runner.invoke(app, ["agent", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    # Dos tareas procesadas
    assert len(calls) == 2
    assert calls[0]["task"] == "primera tarea"
    assert calls[0]["messages"] is None  # primer turno sin contexto
    assert calls[1]["task"] == "segunda tarea"
    assert calls[1]["messages"] is not None  # contexto encadenado del turno 1
    assert calls[1]["messages"][-1]["content"] == "respuesta a primera tarea"


def test_agent_interactive_new_resets_context(monkeypatch, tmp_path):
    """/new reinicia el contexto del agente."""
    from rinari import cli as cli_mod
    from rinari.agent import loop as loop_mod

    inputs = iter(["tarea 1", "/new", "tarea 2", "/exit"])
    calls = []

    def fake_run_agent(task, client, cwd, auto_approve, max_iterations, render_callback, messages=None, **kwargs):
        calls.append({"task": task, "messages": messages})
        prev = list(messages) if messages else []
        return {
            "status": "done",
            "final": f"respuesta a {task}",
            "steps": [],
            "iterations": 1,
            "messages": prev
            + [
                {"role": "user", "content": f"Tarea: {task}"},
                {"role": "assistant", "content": f"respuesta a {task}"},
            ],
        }

    monkeypatch.setattr(loop_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod.console, "input", lambda prompt="": next(inputs))

    result = runner.invoke(app, ["agent", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert len(calls) == 2
    assert calls[0]["messages"] is None
    assert calls[1]["messages"] is None  # /new limpió el contexto


def test_bare_rinari_enters_interactive(monkeypatch, tmp_path):
    """rinari sin subcomando → entra al modo interactivo agéntico."""
    from rinari import cli as cli_mod
    from rinari.agent import loop as loop_mod

    inputs = iter(["/exit"])
    monkeypatch.setattr(cli_mod.console, "input", lambda prompt="": next(inputs))

    result = runner.invoke(app, ["--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert "modo interactivo" in result.stdout.lower() or "rinari" in result.stdout.lower()


def test_normalize_cwd_msys_path():
    """/c/Users/x → C:\\Users\\x (estilo MSYS a Windows)."""
    from pathlib import Path

    from rinari.cli import _normalize_cwd

    p = _normalize_cwd(Path("/c/Users/Test/Proyecto"))
    assert str(p).startswith("C:\\Users\\Test\\Proyecto") or str(p).startswith("C:/Users/Test/Proyecto")


def test_normalize_cwd_tilde():
    """~/proyecto → <home>/proyecto."""
    from pathlib import Path

    from rinari.cli import _normalize_cwd

    p = _normalize_cwd(Path("~/proyecto"))
    assert str(p).endswith("proyecto")
    assert str(Path.home()) in str(p)


def test_normalize_cwd_windows_path():
    """C:\\Users\\x se mantiene igual."""
    from pathlib import Path

    from rinari.cli import _normalize_cwd

    p = _normalize_cwd(Path("C:\\Users\\Test\\Proyecto"))
    assert "C:\\Users\\Test\\Proyecto" in str(p)
