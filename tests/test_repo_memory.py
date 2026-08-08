"""Tests de la memoria por repo (RINARI.md, estilo CLAUDE.md)."""

import json

import pytest

from rinari.agent.loop import run_agent
from rinari.agent.prompt import build_agent_messages


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


class NoopRegistry:
    def openai_schemas(self):
        return []

    def execute(self, name, args, cwd):
        return {"ok": True}


def test_load_repo_memory_from_cwd(tmp_path):
    from rinari.agent.prompt import load_repo_memory

    (tmp_path / "RINARI.md").write_text(
        "# RINARI\n\nUsa pytest para los tests.\n", encoding="utf-8"
    )
    memory = load_repo_memory(str(tmp_path))
    assert memory is not None
    assert "pytest" in memory


def test_load_repo_memory_from_parent(tmp_path):
    """Busca en ancestros (repo raíz aunque el cwd sea un subdir)."""
    from rinari.agent.prompt import load_repo_memory

    (tmp_path / "RINARI.md").write_text("convención del repo\n", encoding="utf-8")
    subdir = tmp_path / "src" / "rinari"
    subdir.mkdir(parents=True)
    memory = load_repo_memory(str(subdir))
    assert memory is not None
    assert "convención" in memory


def test_load_repo_memory_missing(tmp_path):
    from rinari.agent.prompt import load_repo_memory

    assert load_repo_memory(str(tmp_path)) is None


def test_build_agent_messages_includes_memory(tmp_path):
    (tmp_path / "RINARI.md").write_text(
        "Usa uv run pytest. Nunca edites soul.md.\n", encoding="utf-8"
    )
    msgs = build_agent_messages("tarea x", cwd=str(tmp_path))
    system = msgs[0]["content"]
    assert "Usa uv run pytest" in system
    assert "Nunca edites soul.md" in system


def test_build_agent_messages_without_memory(tmp_path):
    msgs = build_agent_messages("tarea x", cwd=str(tmp_path))
    system = msgs[0]["content"]
    assert "Tarea:" not in system  # el system prompt no lleva la tarea


def test_run_agent_sends_memory_to_model(tmp_path):
    """El modelo recibe la memoria del repo en el system prompt."""
    (tmp_path / "RINARI.md").write_text("Regla del repo: tests con pytest.\n", encoding="utf-8")
    client = ScriptedClient([final_msg("ok")])
    result = run_agent(
        "haz algo", client, cwd=str(tmp_path), auto_approve=True,
        registry=NoopRegistry(), max_iterations=2, reminder_threshold=0,
    )
    assert result["status"] == "done"
    system = client.requests[0][0]["content"]
    assert "Regla del repo: tests con pytest" in system


def test_run_agent_no_memory_when_missing(tmp_path):
    client = ScriptedClient([final_msg("ok")])
    result = run_agent(
        "haz algo", client, cwd=str(tmp_path), auto_approve=True,
        registry=NoopRegistry(), max_iterations=2, reminder_threshold=0,
    )
    assert result["status"] == "done"
    system = client.requests[0][0]["content"]
    assert "RINARI" not in system
