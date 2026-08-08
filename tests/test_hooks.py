"""Tests de hooks: scripts pre/post tool del agente (estilo Claude Code)."""

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


def test_load_hooks_from_file(tmp_path):
    """Carga hooks de ~/.rinari/hooks.toml."""
    from rinari.agent.loop import load_hooks

    hooks_dir = tmp_path / ".rinari"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.toml").write_text(
        '[pre_tool]\nedit_file = "echo pre"\n\n[post_tool]\nedit_file = "echo post"\n',
        encoding="utf-8",
    )
    hooks = load_hooks(config_dir=hooks_dir)
    assert hooks["pre_tool"]["edit_file"] == "echo pre"
    assert hooks["post_tool"]["edit_file"] == "echo post"


def test_load_hooks_missing_file_returns_empty(tmp_path):
    from rinari.agent.loop import load_hooks

    assert load_hooks(config_dir=tmp_path / ".rinari") == {}


def test_hooks_run_around_tool(workdir, monkeypatch, tmp_path):
    """Pre/post hooks se ejecutan alrededor de cada tool (marcador en archivo)."""
    from rinari.agent import loop as loop_mod
    from rinari.agent.loop import load_hooks

    marker = tmp_path / "hook.log"
    monkeypatch.setattr(
        loop_mod,
        "load_hooks",
        lambda config_dir=None: {
            "pre_tool": {"edit_file": f'echo "PRE" >> {marker}'},
            "post_tool": {"edit_file": f'echo "POST" >> {marker}'},
        },
    )
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "edit_file", "arguments": {"path": "a.txt", "old": "x", "new": "y"}}]),
            final_msg("listo"),
        ]
    )
    result = run_agent("edita a.txt", client, cwd=workdir, auto_approve=True)
    assert result["status"] == "done"
    log = [ln.strip().strip('"') for ln in marker.read_text(encoding="utf-8").splitlines()]
    assert log == ["PRE", "POST"]


def test_hooks_run_before_tool_only(workdir, monkeypatch, tmp_path):
    """Solo pre_tool cuando no hay post definido."""
    from rinari.agent import loop as loop_mod

    marker = tmp_path / "hook.log"
    monkeypatch.setattr(
        loop_mod,
        "load_hooks",
        lambda config_dir=None: {"pre_tool": {"read_file": f'echo "PRE" >> {marker}'}},
    )
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]),
            final_msg("ok"),
        ]
    )
    result = run_agent("lee a.txt", client, cwd=workdir, auto_approve=True)
    assert result["status"] == "done"
    log = [ln.strip().strip('"') for ln in marker.read_text(encoding="utf-8").splitlines()]
    assert log == ["PRE"]


def test_hook_failure_does_not_block_agent(workdir, monkeypatch):
    """Un hook que falla no rompe el loop (solo se registra)."""
    from rinari.agent import loop as loop_mod

    monkeypatch.setattr(
        loop_mod,
        "load_hooks",
        lambda config_dir=None: {"pre_tool": {"*": "exit 1"}},
    )
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]),
            final_msg("ok"),
        ]
    )
    result = run_agent("tarea", client, cwd=workdir, auto_approve=True)
    assert result["status"] == "done"
    # el fallo del hook se registra como paso
    hook_steps = [s for s in result["steps"] if s["type"] == "hook"]
    assert hook_steps


def test_hooks_wildcard_applies_to_all_tools(workdir, monkeypatch, tmp_path):
    """'*' como tool aplica el hook a cualquier herramienta."""
    from rinari.agent import loop as loop_mod

    marker = tmp_path / "hook.log"
    monkeypatch.setattr(
        loop_mod,
        "load_hooks",
        lambda config_dir=None: {"pre_tool": {"*": f'echo "ALL" >> {marker}'}},
    )
    client = ScriptedClient(
        [
            tool_call_msg([{"id": "c1", "name": "list_dir", "arguments": {"path": "."}}]),
            final_msg("ok"),
        ]
    )
    result = run_agent("tarea", client, cwd=workdir, auto_approve=True)
    assert result["status"] == "done"
    assert "ALL" in marker.read_text(encoding="utf-8")
