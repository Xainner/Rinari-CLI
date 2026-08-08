"""Tests del auto-compact: compactar contexto automáticamente cerca del límite."""

import json

import pytest

from rinari.agent.loop import estimate_tokens, run_agent


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
    """Client con respuestas pre-programadas + contador de requests."""

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


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_estimate_tokens_basic():
    msgs = [{"role": "user", "content": "hola" * 100}]  # 400 chars
    # ~chars/4 → ~100 tokens (con overhead)
    assert estimate_tokens(msgs) > 0


def test_estimate_tokens_empty():
    assert estimate_tokens([]) == 0


def test_auto_compact_triggers_and_resumes(workdir):
    """Con contexto grande + auto_compact, el agente compacta y sigue."""
    # respuestas: compact1, tool call, compact2, final
    client = ScriptedClient(
        [
            "resumen de la conversación",
            tool_call_msg([{"id": "c1", "name": "git_status", "arguments": {}}]),
            "segundo resumen",
            final_msg("listo tras compactar"),
        ]
    )

    # umbral pequeño para forzar compact
    result = run_agent(
        "tarea larga", client, cwd=workdir, auto_approve=True,
        registry=NoopRegistry(), max_iterations=4, reminder_threshold=0,
        auto_compact=True, compact_threshold=50,
    )
    assert result["status"] == "done"

    # el compact hizo una llamada extra de resumen
    compact_msgs = client.requests[0]
    assert any("Resume la conversación" in m.get("content", "") for m in compact_msgs)


def test_auto_compact_off_by_default(workdir):
    """Sin auto_compact, no hay llamada de resumen."""
    client = ScriptedClient([final_msg("directo")])
    result = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        registry=NoopRegistry(), max_iterations=2, reminder_threshold=0,
    )
    assert result["status"] == "done"
    assert len(client.requests) == 1  # sin compact


def test_auto_compact_records_step(workdir):
    """El compact queda registrado en los steps."""
    client = ScriptedClient(
        ["resumen", final_msg("ok")]
    )
    result = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        registry=NoopRegistry(), max_iterations=2, reminder_threshold=0,
        auto_compact=True, compact_threshold=10,
    )
    types = [s.get("type") for s in result["steps"]]
    assert "compact" in types
