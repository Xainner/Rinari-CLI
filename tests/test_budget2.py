"""Tests del presupuesto mejorado: continuación + config [agent]."""

import json

import pytest

from rinari.agent.loop import run_agent


def tool_call_msg(tool_calls: list[dict], content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc.get("id", "call_1"),
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))},
            }
            for tc in tool_calls
        ],
    }


def final_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


class ScriptedClient:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.requests: list[list[dict]] = []

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.requests.append(list(messages))
        if not self.responses:
            raise AssertionError("No hay más respuestas scripteadas")
        return self.responses.pop(0)

    def chat_stream(self, messages, temperature=0.7, max_tokens=None, tools=None):
        raise NotImplementedError


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_continue_with_messages_resumes_context(workdir):
    """Re-llamar run_agent con messages de un run agotado continúa donde quedó."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "git_status", "arguments": {}}]),
            tool_call_msg([{"id": "c2", "name": "git_status", "arguments": {}}]),
            tool_call_msg([{"id": "c3", "name": "git_status", "arguments": {}}]),
            final_msg("terminé la segunda tanda"),
        ]
    )
    # Primera tanda: se agota a las 2 iteraciones (solo tool_calls, sin final)
    first = run_agent(
        "haz algo largo", client, cwd=workdir, auto_approve=True,
        max_iterations=2, reminder_threshold=0,
    )
    assert first["status"] == "max_iterations"
    assert len(first["messages"]) > 3

    # Continuación: misma tarea, messages previos, más presupuesto
    second = run_agent(
        "haz algo largo", client, cwd=workdir, auto_approve=True,
        max_iterations=4, reminder_threshold=0,
        messages=first["messages"],
    )
    assert second["status"] == "done"
    assert second["final"] == "terminé la segunda tanda"
    # el contexto de la primera tanda se preservó (el modelo vio los steps)
    combined = client.requests[2]  # primera llamada de la continuación
    assert any(m["role"] == "tool" for m in combined)


def test_continue_accumulates_iterations(workdir):
    """Las iteraciones de la continuación suman a las previas."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "git_status", "arguments": {}}]),
            final_msg("hecho"),
        ]
    )
    first = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        max_iterations=1, reminder_threshold=0,
    )
    assert first["status"] == "max_iterations"
    second = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        max_iterations=2, reminder_threshold=0,
        messages=first["messages"],
    )
    assert second["status"] == "done"
    assert second["iterations"] == 1
    assert first["iterations"] + second["iterations"] == 2


def test_profile_extra_parses_max_iterations(tmp_path):
    """El parser guarda claves desconocidas del perfil en extra."""
    from rinari.config import load_config

    (tmp_path / ".rinari").mkdir()
    (tmp_path / ".rinari" / "config.toml").write_text(
        '[default]\nbase_url = "http://x/v1"\nmodel = "m"\n'
        '[profile.casa]\nbase_url = "http://x/v1"\nmodel = "m"\n'
        'max_iterations = 30\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / ".rinari")
    prof = cfg.get_profile("casa")
    assert prof.extra.get("max_iterations") == 30


def test_default_profile_extra_empty(tmp_path):
    from rinari.config import load_config

    (tmp_path / ".rinari").mkdir()
    (tmp_path / ".rinari" / "config.toml").write_text(
        '[default]\nbase_url = "http://x/v1"\nmodel = "m"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / ".rinari")
    assert cfg.default.extra == {}
