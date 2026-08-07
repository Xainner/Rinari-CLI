"""Tests para el módulo de configuración con perfiles."""

import os
from pathlib import Path

import pytest

from rinari.config import ConfigError, Profile, load_config, save_config

DEFAULT_TOML = """
[default]
base_url = "http://192.168.0.3:8020/v1"
model = "qwen3.6-27b"
temperature = 0.7

[profile.casa]
base_url = "http://192.168.0.3:8020/v1"
model = "qwen3.6-27b"

[profile.sat]
base_url = "https://api.xainner.com/v1"
api_key = "${SAT_KEY}"
model = "qwen3.6-27b"
temperature = 0.3
"""


@pytest.fixture
def config_dir(tmp_path):
    """Crea un dir de config con un config.toml de prueba."""
    (tmp_path / "config.toml").write_text(DEFAULT_TOML, encoding="utf-8")
    return tmp_path


def test_default_profile_has_defaults(config_dir):
    cfg = load_config(config_dir)
    p = cfg.get_profile("casa")
    assert isinstance(p, Profile)
    assert p.base_url == "http://192.168.0.3:8020/v1"
    assert p.model == "qwen3.6-27b"
    assert p.temperature == 0.7  # hereda del default


def test_custom_profile_overrides_default(config_dir, monkeypatch):
    monkeypatch.setenv("SAT_KEY", "sk-test-123")
    cfg = load_config(config_dir)
    p = cfg.get_profile("sat")
    assert p.base_url == "https://api.xainner.com/v1"
    assert p.temperature == 0.3


def test_api_key_env_expansion(config_dir, monkeypatch):
    monkeypatch.setenv("SAT_KEY", "sk-test-123")
    cfg = load_config(config_dir)
    p = cfg.get_profile("sat")
    assert p.api_key == "sk-test-123"


def test_missing_env_var_raises(config_dir, monkeypatch):
    monkeypatch.delenv("SAT_KEY", raising=False)
    cfg = load_config(config_dir)
    with pytest.raises(ConfigError, match="SAT_KEY"):
        cfg.get_profile("sat")


def test_unknown_profile_raises(config_dir):
    cfg = load_config(config_dir)
    with pytest.raises(ConfigError, match="no existe"):
        cfg.get_profile("nope")


def test_missing_config_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path)
    p = cfg.get_profile("default")
    assert p.base_url  # tiene base_url por defecto
    assert p.api_key is None  # sin key


def test_save_and_reload(tmp_path):
    save_config(
        tmp_path,
        {
            "casa": Profile(
                base_url="http://192.168.0.3:8020/v1",
                model="qwen3.6-27b",
                api_key=None,
                temperature=0.5,
            )
        },
    )
    cfg = load_config(tmp_path)
    p = cfg.get_profile("casa")
    assert p.base_url == "http://192.168.0.3:8020/v1"
    assert p.temperature == 0.5
