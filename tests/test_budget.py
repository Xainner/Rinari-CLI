"""Tests del budget de iteraciones estilo Codex: reminders al modelo + mensaje rico."""

import json

from rinari.agent.loop import run_agent
from rinari.agent.tools import ToolRegistry


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


class RecordingRegistry(ToolRegistry):
    def __init__(self):
        super().__init__(delegate=False)
        self.calls = []

    def execute(self, name, args, cwd):
        self.calls.append(name)
        return {"ok": True, "result": "ok"}


def test_reminder_injected_when_budget_low():
    """Cuando quedan pocas iteraciones, el modelo recibe el aviso."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "list_dir", "arguments": {"path": "."}}]),
            tool_call_msg([{"id": "c2", "name": "list_dir", "arguments": {"path": "."}}]),
            tool_call_msg([{"id": "c3", "name": "list_dir", "arguments": {"path": "."}}]),
            tool_call_msg([{"id": "c4", "name": "list_dir", "arguments": {"path": "."}}]),
        ]
    )
    result = run_agent(
        "tarea", client, cwd="/tmp", auto_approve=True,
        max_iterations=4, reminder_threshold=2,
    )
    assert result["status"] == "max_iterations"
    # el reminder debe aparecer cuando quedaban <=2 iteraciones (en cualquier
    # mensaje — se inyecta en el system message existente)
    assert not any("te quedan" in m.get("content", "") for m in client.requests[0])
    assert any("te quedan" in m.get("content", "") for m in client.requests[1])
    assert any("te quedan" in m.get("content", "") for m in client.requests[2])


def test_reminder_not_injected_when_plenty():
    """Con presupuesto holgado no se inyecta reminder."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "list_dir", "arguments": {"path": "."}}]),
            final_msg("listo"),
        ]
    )
    result = run_agent(
        "tarea", client, cwd="/tmp", auto_approve=True,
        max_iterations=10, reminder_threshold=3,
    )
    assert result["status"] == "done"
    for req in client.requests:
        assert not any("te quedan" in m.get("content", "") for m in req)


def test_result_includes_iteration_stats():
    """El resultado reporta iteraciones y tools ejecutadas."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "git_status", "arguments": {}}]),
            final_msg("hecho"),
        ]
    )
    result = run_agent("tarea", client, cwd="/tmp", auto_approve=True, max_iterations=10)
    assert result["status"] == "done"
    assert result["iterations"] == 2
    assert result["tool_count"] == 1


def test_max_iterations_result_has_summary():
    """Al cortar por límite, el resultado incluye un resumen accionable."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "git_status", "arguments": {}}]),
            tool_call_msg([{"id": "c2", "name": "git_status", "arguments": {}}]),
        ]
    )
    result = run_agent(
        "tarea", client, cwd="/tmp", auto_approve=True,
        max_iterations=2, reminder_threshold=1,
    )
    assert result["status"] == "max_iterations"
    assert "iterations" in result
    assert result["tool_count"] == 2
    # el último paso queda registrado
    assert result["steps"][-1]["type"] in ("tool_result", "max_iterations")
