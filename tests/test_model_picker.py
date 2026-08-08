"""Tests del picker de modelo con cambio de provider (estilo hermes model)."""

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari import cli
from rinari import config as config_mod
from rinari.cli import app

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirige Path.home a tmp_path (config aislado)."""
    (tmp_path / ".rinari").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def write_default_config(home: Path, provider: str = "local"):
    """Config mínimo con un perfil default."""
    (home / ".rinari" / "config.toml").write_text(
        f'[default]\nbase_url = "http://x/v1"\nmodel = "m-actual"\nprovider = "{provider}"\n',
        encoding="utf-8",
    )


def test_picker_asks_provider_change_and_lists_models(fake_home, monkeypatch):
    """El picker pregunta si cambiar de provider y lista los del endpoint."""
    write_default_config(fake_home)
    monkeypatch.setattr(cli, "_model_list_models", lambda base_url, api_key, provider: [
        {"id": "m1", "owned_by": "x", "created": 1},
        {"id": "m2", "owned_by": "x", "created": 1},
    ])
    # pregunta cambiar provider → "n" (no) → elige modelo índice 1 (m2)
    result = runner.invoke(app, ["model"], input="n\n1\n")
    assert result.exit_code == 0
    assert "Modelos en 'default'" in result.stdout
    cfg = config_mod.load_config(fake_home / ".rinari")
    assert cfg.get_profile("default").model == "m2"


def test_picker_switches_provider_and_model(fake_home, monkeypatch):
    """Cambiar de provider actualiza base_url/key y persiste ambos."""
    write_default_config(fake_home)
    chosen: dict = {}

    def fake_list(base_url, api_key, provider):
        chosen["base_url"] = base_url
        chosen["api_key"] = api_key
        chosen["provider"] = provider
        return [{"id": "claude-x", "owned_by": "anthropic", "created": 1}]

    monkeypatch.setattr(cli, "_model_list_models", fake_list)
    # cambiar provider → "s" → elige provider 1 (anthropic) → endpoint default →
    # sin key → modelo 0 (claude-x)
    result = runner.invoke(app, ["model"], input="s\n1\n\n\n0\n")
    assert result.exit_code == 0
    assert chosen["provider"] == "anthropic"
    assert chosen["base_url"] == "https://api.anthropic.com/v1"
    cfg = config_mod.load_config(fake_home / ".rinari")
    prof = cfg.get_profile("default")
    assert prof.model == "claude-x"
    assert prof.provider == "anthropic"
    assert prof.base_url == "https://api.anthropic.com/v1"


def test_picker_provider_keeps_current_endpoint(fake_home, monkeypatch):
    """Sin cambio de provider: usa el endpoint actual del perfil."""
    write_default_config(fake_home)
    chosen: dict = {}

    def fake_list(base_url, api_key, provider):
        chosen["base_url"] = base_url
        return [{"id": "m9", "owned_by": "x", "created": 1}]

    monkeypatch.setattr(cli, "_model_list_models", fake_list)
    result = runner.invoke(app, ["model"], input="\n0\n")
    assert result.exit_code == 0
    assert chosen["base_url"] == "http://x/v1"


def test_picker_invalid_provider_choice(fake_home, monkeypatch):
    """Un provider inválido aborta con error claro."""
    write_default_config(fake_home)
    result = runner.invoke(app, ["model"], input="s\n99\n")
    assert result.exit_code == 1
    assert "proveedor" in result.stdout.lower() or "provider" in result.stdout.lower()
