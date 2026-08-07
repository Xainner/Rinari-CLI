"""Tests para el agent loop con tool calling."""

import json

import httpx
import pytest

from rinari.agent.loop import AgentError, run_agent
from rinari.client import LLMClient


def tool_call_msg(tool_calls: list[dict], content: str = "") -> dict:
    """Construye un assistant message con tool_calls."""
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
    """Cliente falso que responde con una secuencia predefinida de mensajes."""

    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.requests: list[list[dict]] = []  # mensajes enviados en cada llamada

    def chat_stream(self, messages, temperature=0.7, max_tokens=None, tools=None):
        # chat_stream debe devolver un iterable; para el loop usamos
        # completar el mensaje completo de una vez
        raise NotImplementedError

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.requests.append(messages)
        if not self.responses:
            raise AssertionError("No hay más respuestas scripteadas")
        return self.responses.pop(0)


class RecordingRegistry:
    """Registro falso que registra ejecuciones y devuelve resultados fijos."""

    def __init__(self, results: dict | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []
        self.default = {"ok": True, "result": "ok"}

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": "run_command", "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args, cwd):
        self.calls.append((name, args))
        return self.results.get(name, self.default)


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_agent_completes_after_tool_roundtrip(workdir):
    """Modelo pide tool → resultado → respuesta final."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "echo hi"}}]),
            final_msg("Listo, ejecuté el comando."),
        ]
    )
    registry = RecordingRegistry({"run_command": {"ok": True, "stdout": "hi\n", "exit_code": 0}})

    result = run_agent(
        task="ejecuta algo",
        client=client,
        cwd=workdir,
        registry=registry,
        auto_approve=True,
        max_iterations=5,
    )

    assert result["status"] == "done"
    assert result["final"] == "Listo, ejecuté el comando."
    # El resultado del tool se devolvió al modelo en la segunda llamada
    second_call = client.requests[1]
    last = second_call[-1]
    assert last["role"] == "tool"
    assert "hi" in last["content"]


def test_agent_loops_multiple_tools(workdir):
    """Dos rondas de tools antes de la respuesta final."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]),
            tool_call_msg([{"id": "c2", "name": "write_file", "arguments": {"path": "b.txt", "content": "x"}}]),
            final_msg("Hecho."),
        ]
    )
    registry = RecordingRegistry(
        {
            "read_file": {"ok": True, "content": "contenido de a"},
            "write_file": {"ok": True, "path": "b.txt"},
        }
    )

    result = run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=True, max_iterations=5,
    )
    assert result["status"] == "done"
    assert registry.calls[0][0] == "read_file"
    assert registry.calls[1][0] == "write_file"


def test_agent_stops_at_max_iterations(workdir):
    """El modelo pide tools infinitamente → corta en max_iterations."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": f"c{i}", "name": "run_command", "arguments": {"command": "x"}}])
            for i in range(10)
        ]
    )
    registry = RecordingRegistry({"run_command": {"ok": True, "stdout": "", "exit_code": 0}})

    result = run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=True, max_iterations=3,
    )
    assert result["status"] == "max_iterations"
    assert len(client.requests) <= 4  # 3 iteraciones + posible extra


def test_agent_tool_error_returned_to_model(workdir):
    """Un error de tool se devuelve como observación al modelo."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "read_file", "arguments": {"path": "no.txt"}}]),
            final_msg("El archivo no existe, lo siento."),
        ]
    )
    registry = RecordingRegistry({"read_file": {"ok": False, "error": "no existe"}})

    result = run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=True, max_iterations=5,
    )
    second_call = client.requests[1]
    last = second_call[-1]
    assert last["role"] == "tool"
    assert "no existe" in last["content"]


def test_agent_denied_command_not_executed(workdir):
    """Sin auto_approve y con aprobador que niega → el comando no se ejecuta."""
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "rm -rf x"}}]),
            final_msg("Cancelado."),
        ]
    )
    registry = RecordingRegistry({"run_command": {"ok": True, "stdout": "", "exit_code": 0}})

    result = run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=False,
        approver=lambda name, args, cwd: False,  # niega todo
        max_iterations=5,
    )
    assert registry.calls == []  # nunca se ejecutó
    # El modelo recibió un tool result de "denegado"
    second_call = client.requests[1]
    last = second_call[-1]
    assert "denegad" in last["content"].lower() or "negad" in last["content"].lower()


def test_agent_auto_approve_skips_approver(workdir):
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "echo ok"}}]),
            final_msg("ok"),
        ]
    )
    registry = RecordingRegistry()
    approved = []

    def approver(name, args, cwd):
        approved.append(name)
        return True

    run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=True, approver=approver, max_iterations=5,
    )
    assert approved == []  # auto_approve no consulta al aprobador


def test_agent_requires_approval_for_dangerous_without_auto(workdir, monkeypatch):
    """Comando peligroso sin auto_approve → pasa por el aprobador."""
    from rinari.agent import loop

    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "sudo rm -rf /"}}]),
            final_msg("hecho"),
        ]
    )
    registry = RecordingRegistry()
    seen = {}

    def approver(name, args, cwd):
        seen["cmd"] = args.get("command")
        return True

    run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=False, approver=approver, max_iterations=5,
    )
    assert "sudo rm -rf /" in seen["cmd"]


def test_agent_tracks_steps(workdir):
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "echo hi"}}]),
            final_msg("done"),
        ]
    )
    registry = RecordingRegistry()
    result = run_agent(
        task="tarea", client=client, cwd=workdir, registry=registry,
        auto_approve=True, max_iterations=5,
    )
    assert len(result["steps"]) >= 2
    assert result["steps"][0]["type"] == "tool_call"
    assert result["steps"][-1]["type"] == "final"
