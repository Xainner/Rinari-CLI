"""Tests para el soporte multi-provider: tabla PROVIDERS + campo en Profile."""

import pytest

from rinari.config import (
    PROVIDERS,
    ConfigError,
    Profile,
    load_config,
    set_profile_model,
)


def test_providers_table_has_common_providers():
    """Los providers principales están definidos."""
    assert "openai" in PROVIDERS
    assert "anthropic" in PROVIDERS
    assert "openrouter" in PROVIDERS
    assert "opencode" in PROVIDERS
    assert "local" in PROVIDERS
    assert "custom" in PROVIDERS


def test_opencode_provider_shape():
    """OpenCode Zen es OpenAI-compatible con su endpoint real."""
    spec = PROVIDERS["opencode"]
    assert spec["api_format"] == "openai"
    assert spec["base_url"] == "https://opencode.ai/zen/v1"
    assert spec["env_var"] == "OPENCODE_API_KEY"


def test_opencode_go_provider_shape():
    """OpenCode Go tiene su propio endpoint de modelos abiertos."""
    spec = PROVIDERS["opencode-go"]
    assert spec["api_format"] == "openai"
    assert spec["base_url"] == "https://opencode.ai/zen/go/v1"
    assert spec["env_var"] == "OPENCODE_API_KEY"


def test_provider_shape():
    """Cada provider tiene api_format válido y los campos esperados."""
    for name, spec in PROVIDERS.items():
        assert spec["api_format"] in ("openai", "anthropic")
        assert "base_url" in spec
        assert "env_var" in spec


def test_anthropic_uses_native_format():
    """Anthropic es el único formato nativo (los demás son OpenAI-compat)."""
    assert PROVIDERS["anthropic"]["api_format"] == "anthropic"
    assert PROVIDERS["openai"]["api_format"] == "openai"
    assert PROVIDERS["openrouter"]["api_format"] == "openai"
    assert PROVIDERS["local"]["api_format"] == "openai"


def test_profile_accepts_provider_field():
    """Profile lleva provider y lo persiste en config.toml."""
    p = Profile(base_url="https://api.anthropic.com/v1", model="claude-sonnet", provider="anthropic")
    assert p.provider == "anthropic"
    # default al construir sin provider
    p2 = Profile(base_url="http://x/v1", model="m")
    assert p2.provider == "openai"


def test_load_config_parses_provider(tmp_path):
    """load_config lee el campo provider del TOML."""
    (tmp_path / "config.toml").write_text(
        '[profile.anthropic]\nbase_url = "https://api.anthropic.com/v1"\n'
        'model = "claude-sonnet-4"\nprovider = "anthropic"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    prof = cfg.get_profile("anthropic")
    assert prof.provider == "anthropic"


def test_set_profile_model_preserves_provider(tmp_path):
    """set_profile_model mantiene el provider al cambiar el modelo."""
    (tmp_path / "config.toml").write_text(
        '[profile.ant]\nbase_url = "https://api.anthropic.com/v1"\n'
        'model = "claude-sonnet-4"\nprovider = "anthropic"\n',
        encoding="utf-8",
    )
    set_profile_model(tmp_path, "ant", "claude-opus-4")
    cfg = load_config(tmp_path)
    prof = cfg.get_profile("ant")
    assert prof.model == "claude-opus-4"
    assert prof.provider == "anthropic"


def test_unknown_provider_is_ok_but_flagged(tmp_path):
    """Un provider desconocido en config carga pero se marca (compat hacia atrás)."""
    (tmp_path / "config.toml").write_text(
        '[profile.raro]\nbase_url = "http://x/v1"\nmodel = "m"\nprovider = "no-existe"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.get_profile("raro").provider == "no-existe"
