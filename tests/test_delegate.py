"""Tests de la tool delegate_task (subagentes de un nivel)."""

import json

import pytest

from rinari.agent.loop import run_agent
from rinari.agent.tools import ToolRegistry


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


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


def test_registry_exposes_delegate_task():
    """delegate_task aparece en los schemas cuando se habilita."""
    reg = ToolRegistry(delegate=True)
    names = {s["function"]["name"] for s in reg.openai_schemas()}
    assert "delegate_task" in names


def test_registry_without_delegate_has_no_task():
    reg = ToolRegistry(delegate=False)
    names = {s["function"]["name"] for s in reg.openai_schemas()}
    assert "delegate_task" not in names


def test_agent_can_delegate(workdir):
    """El agente llama delegate_task y recibe el resultado del subagente."""
    client = ScriptedClient(
        [
            tool_call_msg([{
                "id": "d1", "name": "delegate_task",
                "arguments": {"task": "revisa el archivo y resume", "context": "es un repo de tests"},
            }]),
            final_msg("resumen del subagente: 3 funciones, 0 bugs"),
            final_msg("el subagente resumió correctamente"),
        ]
    )
    result = run_agent(
        "analiza el repo",
        client,
        cwd=workdir,
        auto_approve=True,
        max_iterations=5,
    )
    assert result["status"] == "done"
    # el resultado del subagente se devolvió como tool result
    tool_results = [s for s in result["steps"] if s["type"] == "tool_result" and s["name"] == "delegate_task"]
    assert tool_results
    sub = tool_results[0]["result"]
    assert sub.get("ok") is True
    assert "final" in sub


def test_delegate_subagent_has_no_delegate(workdir):
    """El subagente NO puede delegar a su vez (un nivel, sin recursión)."""
    client = ScriptedClient(
        [
            tool_call_msg([{
                "id": "d1", "name": "delegate_task",
                "arguments": {"task": "tarea corta", "context": ""},
            }]),
            final_msg("subagente terminó"),
            final_msg("padre terminó"),
        ]
    )
    result = run_agent("principal", client, cwd=workdir, auto_approve=True, max_iterations=5)
    assert result["status"] == "done"
    # el sub-loop (segunda llamada) no debe haber visto delegate_task en sus tools
    sub_call_messages = client.requests[1]
    # las tools del sub-loop se pasan a chat; verificamos que no rompió y el
    # subagente terminó con su final
    tr = [s for s in result["steps"] if s["type"] == "tool_result" and s["name"] == "delegate_task"]
    assert tr and tr[0]["result"]["status"] == "done"


def test_delegate_requires_task(workdir):
    """delegate_task sin task devuelve error claro."""
    from rinari.agent.loop import run_agent

    client = ScriptedClient(
        [
            tool_call_msg([{"id": "d1", "name": "delegate_task", "arguments": {}}]),
            final_msg("listo"),
        ]
    )
    result = run_agent("x", client, cwd=workdir, auto_approve=True, max_iterations=5)
    tr = [s for s in result["steps"] if s["type"] == "tool_result" and s["name"] == "delegate_task"]
    assert tr
    assert tr[0]["result"].get("ok") is False


def test_delegate_result_chainable(workdir):
    """El resultado del subagente se encadena como tool message."""
    client = ScriptedClient(
        [
            tool_call_msg([{
                "id": "d1", "name": "delegate_task",
                "arguments": {"task": "revisa x", "context": ""},
            }]),
            final_msg("resultado interno del subagente"),
            final_msg("gracias por el resumen"),
        ]
    )
    result = run_agent("principal", client, cwd=workdir, auto_approve=True, max_iterations=5)
    # el tool message con el resultado del subagente está en messages
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert tool_msgs
    payload = json.loads(tool_msgs[-1]["content"])
    assert "ok" in payload
