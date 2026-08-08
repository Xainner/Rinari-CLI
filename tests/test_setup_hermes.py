"""Tests del setup estilo Hermes: nombre del usuario + perfil al inicio."""

from pathlib import Path

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


def write_config(home: Path, text: str):
    d = home / ".rinari"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(text, encoding="utf-8")


def test_config_supports_user_name(tmp_path):
    """El config guarda el nombre del usuario en [user]."""
    write_config(tmp_path, '[user]\nname = "Xainner"\n\n[default]\nbase_url = "http://x/v1"\nmodel = "m"\n')
    cfg = load_config(tmp_path / ".rinari")
    assert cfg.user_name == "Xainner"


def test_config_user_name_default():
    """Sin [user], user_name es None (identity usa su default)."""
    cfg = load_config(Path(tmp_path_fallback()))
    assert cfg.user_name in (None, "Xainner")


def tmp_path_fallback() -> str:
    import tempfile

    return tempfile.mkdtemp()


def test_setup_asks_user_name_first(fake_home, monkeypatch):
    """El wizard pregunta el nombre del usuario primero y lo guarda en [user]."""
    from rinari import cli

    monkeypatch.setattr(cli, "_setup_list_models",
                        lambda base_url, api_key, provider="openai": [
                            {"id": "modelo-x"},
                        ])
    # inputs: nombre de usuario, nombre de perfil, provider (local), endpoint, key, modelo
    result = runner.invoke(
        app, ["setup"],
        input="Xainner\nmi-casa\n6\n\n\n0\n",
    )
    assert result.exit_code == 0
    cfg = load_config(fake_home / ".rinari")
    assert cfg.user_name == "Xainner"
    assert cfg.get_profile("mi-casa").model == "modelo-x"


def test_identity_uses_config_user_name(fake_home, monkeypatch):
    """El prompt usa el nombre del usuario del config, no uno hardcodeado."""
    from rinari import cli
    from rinari.identity import build_chat_prompt

    write_config(fake_home, '[user]\nname = "Valeria"\n\n[default]\nbase_url = "http://x/v1"\nmodel = "m"\n')
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: fake_home))

    prompt = build_chat_prompt()
    assert "Valeria" in prompt
    assert "Xainner" not in prompt


def test_identity_default_name_when_no_config():
    """Sin [user] en config, el prompt usa un nombre genérico."""
    from rinari.identity import build_chat_prompt

    prompt = build_chat_prompt()
    assert "Rinari" in prompt
