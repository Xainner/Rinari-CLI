"""Tests del sandbox por niveles (estilo Codex CLI)."""

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


class RecordingRegistry:
    def __init__(self, outputs: dict | None = None):
        self.outputs = outputs or {}
        self.calls: list[tuple[str, dict]] = []

    def openai_schemas(self):
        return []

    def execute(self, name, args, cwd):
        self.calls.append((name, args))
        return self.outputs.get(name, {"ok": True, "stdout": "", "exit_code": 0})


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_read_only_blocks_writes(workdir):
    """En read-only, write_file se deniega y NO se ejecuta."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]),
            final_msg("no pude escribir"),
        ]
    )
    result = run_agent(
        "escribe algo", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
        sandbox="read-only",
    )
    assert registry.calls == []  # nunca se ejecutó write_file
    # el modelo vio el error de sandbox
    tool_msgs = [m for m in client.requests[1] if m.get("role") == "tool"]
    assert "read-only" in tool_msgs[0]["content"].lower()


def test_read_only_allows_reads(workdir):
    """En read-only, read_file sí se ejecuta."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]),
            final_msg("leído"),
        ]
    )
    result = run_agent(
        "lee algo", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
        sandbox="read-only",
    )
    assert result["status"] == "done"
    assert ("read_file", {"path": "a.txt"}) in registry.calls


def test_workspace_write_allows_edits(workdir):
    """workspace-write (default): write_file sí pasa sin aprobación."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]),
            final_msg("escrito"),
        ]
    )
    result = run_agent(
        "escribe", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
    )
    assert result["status"] == "done"
    assert ("write_file", {"path": "a.txt", "content": "x"}) in registry.calls


def test_workspace_write_dangerous_asks_approver(workdir):
    """workspace-write: comando peligroso pide aprobación (approver=False → denegado)."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "rm -rf x"}}]),
            final_msg("denegado"),
        ]
    )
    result = run_agent(
        "borra", client, cwd=workdir, auto_approve=False,
        approver=lambda name, args, cwd: False,
        registry=registry, max_iterations=2, reminder_threshold=0,
        sandbox="workspace-write",
    )
    assert registry.calls == []
    tool_msgs = [m for m in client.requests[1] if m.get("role") == "tool"]
    assert "denegad" in tool_msgs[0]["content"].lower()


def test_danger_full_access_skips_approver(workdir):
    """danger-full-access: hasta lo peligroso pasa sin aprobación."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "rm -rf x"}}]),
            final_msg("borrado"),
        ]
    )
    result = run_agent(
        "borra", client, cwd=workdir, auto_approve=False,
        approver=lambda name, args, cwd: False,  # si se llamara, denegaría
        registry=registry, max_iterations=2, reminder_threshold=0,
        sandbox="danger-full-access",
    )
    assert result["status"] == "done"
    assert ("run_command", {"command": "rm -rf x"}) in registry.calls


def test_read_only_blocks_dangerous_command(workdir):
    """read-only también bloquea run_command (aunque no sea de escritura)."""
    registry = RecordingRegistry()
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "echo hi"}}]),
            final_msg("no"),
        ]
    )
    result = run_agent(
        "corre", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
        sandbox="read-only",
    )
    assert registry.calls == []
