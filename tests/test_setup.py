"""Tests del wizard de setup y la gestión de modelos en el CLI."""

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from rinari import config as config_mod
from rinari.cli import app
from rinari.config import ConfigError, load_config

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirige Path.home a tmp_path (config aislado)."""
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def make_client(base_url: str = "http://x/v1", models=None):
    """LLMClient con transport mock que devuelve modelos."""
    from rinari.client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": models or [{"id": "m1"}, {"id": "m2"}]})

    return LLMClient(base_url=base_url, transport=httpx.MockTransport(handler))


def test_pick_model_by_index():
    """pick_model_index valida el índice y devuelve el id del modelo."""
    from rinari.cli import pick_model_index

    models = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert pick_model_index(models, "0") == "a"
    assert pick_model_index(models, "2") == "c"


def test_pick_model_invalid_index():
    """Índice fuera de rango → ConfigError."""
    from rinari.cli import pick_model_index

    models = [{"id": "a"}]
    with pytest.raises(ConfigError):
        pick_model_index(models, "5")
    with pytest.raises(ConfigError):
        pick_model_index(models, "abc")


def test_format_model_list():
    """format_model_list numera los modelos para el wizard."""
    from rinari.cli import format_model_list

    out = format_model_list([{"id": "qwen3.6-27b"}, {"id": "llama-8b"}])
    assert "qwen3.6-27b" in out
    assert "llama-8b" in out
    assert "0" in out and "1" in out


def test_models_command_shows_active(fake_home, monkeypatch):
    """`rinari models` muestra el modelo activo del perfil + lista."""
    from types import SimpleNamespace

    from rinari import cli
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "default", "m1", base_url="http://x/v1")
    prof = SimpleNamespace(model="m1")
    monkeypatch.setattr(cli, "_build_client", lambda name: (make_client(), prof))

    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "m1" in result.output  # activo
    assert "m2" in result.output  # de la lista mock


def test_model_set_updates_config(fake_home):
    """`rinari model set <m>` cambia el modelo del perfil en config.toml."""
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "casa", "antes", base_url="http://x/v1")

    result = runner.invoke(app, ["model", "set", "despues", "--profile", "casa"])
    assert result.exit_code == 0

    cfg = load_config(fake_home / ".rinari")
    assert cfg.get_profile("casa").model == "despues"


def test_model_set_missing_profile(fake_home):
    """`rinari model set` en perfil inexistente → error claro."""
    result = runner.invoke(app, ["model", "set", "x", "--profile", "nope"])
    assert result.exit_code != 0
    assert "no existe" in result.output.lower() or "setup" in result.output.lower()
