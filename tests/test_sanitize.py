"""Tests del sanitizador de tool content (evita imitación de formatos raros)."""

import json

from rinari.agent.loop import _sanitize_tool_content, run_agent


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


class OutputRegistry:
    """Registry que devuelve un output contaminado con @url:`...`."""

    def __init__(self, output: str):
        self.output = output

    def openai_schemas(self):
        return []

    def execute(self, name, args, cwd):
        return {"ok": True, "stdout": self.output, "exit_code": 0}


def test_sanitize_url_backtick():
    raw = 'Error: mira @url:`https://aka.ms/enablevirtualization` para más info'
    clean = _sanitize_tool_content(raw)
    assert "@url:" not in clean
    assert "https://aka.ms/enablevirtualization" in clean


def test_sanitize_url_no_backtick():
    raw = "consulta @url:https://example.com/a?b=1"
    clean = _sanitize_tool_content(raw)
    assert "@url:" not in clean
    assert "https://example.com/a?b=1" in clean


def test_sanitize_plain_text_unchanged():
    raw = "todo normal, sin formatos raros"
    assert _sanitize_tool_content(raw) == raw


def test_sanitize_in_json_output():
    """El tool result con @url dentro de JSON se sanea antes de ir al modelo."""
    registry = OutputRegistry('WSL2 error: @url:`https://aka.ms/enablevirtualization`')
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "run_command", "arguments": {"command": "wsl --status"}}]),
            final_msg("listo"),
        ]
    )
    result = run_agent(
        "revisa wsl", client, cwd="/tmp", auto_approve=True,
        registry=registry, max_iterations=3, reminder_threshold=0,
    )
    assert result["status"] == "done"
    # el tool message que ve el modelo no debe contener @url:
    tool_msgs = [m for m in client.requests[1] if m.get("role") == "tool"]
    assert tool_msgs
    assert "@url:" not in tool_msgs[0]["content"]
    assert "https://aka.ms/enablevirtualization" in tool_msgs[0]["content"]
