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

    out = format_model_list([{"id": "test-model"}, {"id": "llama-8b"}])
    assert "test-model" in out
    assert "llama-8b" in out
    assert "0" in out and "1" in out


def test_pick_provider_by_index():
    """pick_provider devuelve el nombre del provider por índice."""
    from rinari.cli import pick_provider

    assert pick_provider("0") == "openai"
    assert pick_provider("1") == "anthropic"


def test_pick_provider_invalid():
    """Índice inválido → ConfigError con la lista de providers."""
    from rinari.cli import pick_provider

    with pytest.raises(ConfigError):
        pick_provider("99")
    with pytest.raises(ConfigError):
        pick_provider("abc")


def test_provider_listing():
    """format_providers lista los providers numerados con su descripción."""
    from rinari.cli import format_providers

    out = format_providers()
    assert "anthropic" in out
    assert "openrouter" in out
    assert "local" in out
    assert "0" in out


def test_setup_wizard_asks_provider(fake_home, monkeypatch):
    """El wizard pide provider primero y crea el perfil con él."""
    from rinari import cli
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "default", "m0", base_url="http://x/v1")
    monkeypatch.setattr(cli, "_setup_list_models",
                        lambda base_url, api_key, provider="openai": [
                            {"id": "claude-sonnet-4"},
                        ])

    result = runner.invoke(
        app, ["setup", "--name", "ant"],
        input="1\n\n\n0\n",  # provider=anthropic, url default, sin key, modelo 0
    )
    assert result.exit_code == 0
    cfg = load_config(fake_home / ".rinari")
    prof = cfg.get_profile("ant")
    assert prof.provider == "anthropic"
    assert prof.base_url == "https://api.anthropic.com/v1"
    assert prof.model == "claude-sonnet-4"


def test_setup_wizard_local_provider(fake_home, monkeypatch):
    """Provider 'local' usa su base_url por defecto."""
    from rinari import cli
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "default", "m0", base_url="http://x/v1")
    monkeypatch.setattr(cli, "_setup_list_models",
                        lambda base_url, api_key, provider="openai": [
                            {"id": "test-model"},
                        ])

    result = runner.invoke(
        app, ["setup", "--name", "casita"],
        input="6\n\n\n0\n",  # provider=local (índice 6), url default, sin key, modelo 0
    )
    assert result.exit_code == 0
    cfg = load_config(fake_home / ".rinari")
    prof = cfg.get_profile("casita")
    assert prof.provider == "local"


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
