"""Tests del CLI: one-shot run y listado de modelos (con mock transport).

typer.testing.CliRunner ejercita los entrypoints sin TTY.
"""

import json

import httpx
import pytest
from typer.testing import CliRunner

from qwencli.cli import app

runner = CliRunner()


def make_transport_ok(content: str = "respuesta"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-27b"}, {"id": "otro"}]})
        body = json.loads(request.content)
        if body.get("stream"):
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            }
            done = {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            payload = (
                f"data: {json.dumps(chunk)}\n\n"
                f"data: {json.dumps(done)}\n\n"
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, content=payload, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def patch_llm_client(monkeypatch):
    """Fuerza un transport mock en LLMClient para todos los tests del CLI."""
    from qwencli import client as client_mod

    orig_init = client_mod.LLMClient.__init__

    def fake_init(self, base_url, api_key=None, model="qwen3.6-27b", timeout=300.0, transport=None):
        # Inyecta MockTransport SIEMPRE (ignora el real)
        orig_init(self, base_url=base_url, api_key=api_key, model=model, timeout=timeout,
                  transport=make_transport_ok())

    monkeypatch.setattr(client_mod.LLMClient, "__init__", fake_init)


@pytest.fixture(autouse=True)
def fake_home_config(monkeypatch, tmp_path):
    """Config sin key para no depender del HOME real."""
    from qwencli import config as config_mod

    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SAT_KEY", "sk-test")


def test_run_one_shot_streams_to_stdout():
    result = runner.invoke(app, ["run", "hola"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_run_with_profile():
    result = runner.invoke(app, ["run", "hola", "--profile", "default"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_run_no_stream():
    result = runner.invoke(app, ["run", "hola", "--no-stream"])
    assert result.exit_code == 0
    assert "respuesta" in result.stdout


def test_models_lists_ids():
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "qwen3.6-27b" in result.stdout


def test_unknown_profile_fails_cleanly():
    result = runner.invoke(app, ["run", "hola", "--profile", "nope"])
    assert result.exit_code == 1
    assert "no existe" in result.stdout
