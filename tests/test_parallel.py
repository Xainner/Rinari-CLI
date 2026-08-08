"""Tests de parallel tool calls: ejecutar varias tools del turno en paralelo."""

import json
import time

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


class SlowRegistry:
    """Registry cuyas tools tardan 0.3s — para medir concurrencia."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls: list[str] = []

    def openai_schemas(self):
        return []

    def execute(self, name, args, cwd):
        time.sleep(self.delay)
        self.calls.append(name)
        return {"ok": True, "stdout": f"{name} done", "exit_code": 0}


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_parallel_executes_concurrently(workdir):
    """3 tools lentas en paralelo tardan ~1 delay, no 3."""
    registry = SlowRegistry(delay=0.3)
    client = ScriptedClient(
        [
            tool_call_msg([
                {"id": "c1", "name": "read_file", "arguments": {"path": "a"}},
                {"id": "c2", "name": "read_file", "arguments": {"path": "b"}},
                {"id": "c3", "name": "read_file", "arguments": {"path": "c"}},
            ]),
            final_msg("listo"),
        ]
    )
    start = time.monotonic()
    result = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
        parallel_tools=True,
    )
    elapsed = time.monotonic() - start
    assert result["status"] == "done"
    assert elapsed < 0.8  # paralelo: ~0.3s + overhead, no ~0.9s
    assert len(registry.calls) == 3


def test_parallel_keeps_message_order(workdir):
    """Los tool messages vuelven al modelo en el orden de las tool calls."""
    registry = SlowRegistry(delay=0.05)
    client = ScriptedClient(
        [
            tool_call_msg([
                {"id": "c1", "name": "read_file", "arguments": {"path": "a"}},
                {"id": "c2", "name": "read_file", "arguments": {"path": "b"}},
            ]),
            final_msg("listo"),
        ]
    )
    result = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
        parallel_tools=True,
    )
    assert result["status"] == "done"
    tool_msgs = [m for m in client.requests[1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]


def test_sequential_default(workdir):
    """Sin parallel_tools, todo sigue secuencial (compat)."""
    registry = SlowRegistry(delay=0.05)
    client = ScriptedClient(
        [
            tool_call_msg([
                {"id": "c1", "name": "read_file", "arguments": {"path": "a"}},
                {"id": "c2", "name": "read_file", "arguments": {"path": "b"}},
            ]),
            final_msg("listo"),
        ]
    )
    start = time.monotonic()
    result = run_agent(
        "tarea", client, cwd=workdir, auto_approve=True,
        registry=registry, max_iterations=2, reminder_threshold=0,
    )
    elapsed = time.monotonic() - start
    assert result["status"] == "done"
    assert elapsed >= 0.09  # secuencial: ~0.1s
