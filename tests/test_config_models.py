"""Tests para set_profile_model: cambiar el modelo de un perfil y guardar."""

import pytest

from rinari.config import Config, ConfigError, Profile, load_config, set_profile_model


def test_set_profile_model_updates_and_saves(tmp_path):
    """Cambia el modelo del perfil y lo persiste en config.toml."""
    (tmp_path / "config.toml").write_text(
        '[profile.casa]\nbase_url = "http://x/v1"\nmodel = "viejo"\n',
        encoding="utf-8",
    )
    path = set_profile_model(tmp_path, "casa", "nuevo-modelo")
    assert path.exists()

    cfg = load_config(tmp_path)
    profile = cfg.get_profile("casa")
    assert profile.model == "nuevo-modelo"
    # el archivo quedó con el nuevo modelo
    content = path.read_text(encoding="utf-8")
    assert 'model = "nuevo-modelo"' in content


def test_set_profile_model_default(tmp_path):
    """Sin perfil nombrado → actualiza [default]."""
    (tmp_path / "config.toml").write_text(
        'base_url = "http://x/v1"\nmodel = "viejo"\n',
        encoding="utf-8",
    )
    set_profile_model(tmp_path, "default", "otro")
    cfg = load_config(tmp_path)
    assert cfg.get_profile("default").model == "otro"


def test_set_profile_model_missing_profile(tmp_path):
    """Perfil inexistente → ConfigError."""
    (tmp_path / "config.toml").write_text(
        '[profile.casa]\nbase_url = "http://x/v1"\nmodel = "m"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        set_profile_model(tmp_path, "nope", "x")


def test_set_profile_model_creates_config(tmp_path):
    """Sin config.toml → lo crea con el perfil."""
    path = set_profile_model(tmp_path, "nuevo", "m1", base_url="http://y/v1")
    assert path.exists()
    cfg = load_config(tmp_path)
    assert cfg.get_profile("nuevo").model == "m1"
    assert cfg.get_profile("nuevo").base_url == "http://y/v1"
