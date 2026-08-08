"""Configuración de rinari: perfiles con base_url, api_key, model, temperature.

Los perfiles viven en ~/.rinari/config.toml. Soporta:
- Sección [default] cuyos valores heredan los perfiles.
- ${ENV_VAR} expansion en api_key y cualquier campo string.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_CONFIG = {
    "default": {
        "base_url": "http://localhost:8080/v1",
        "model": "local-model",
        "temperature": 0.7,
        "api_key": None,
        "provider": "local",
    }
}

# Providers soportados: api_format define cómo habla el cliente.
# - "openai": POST /chat/completions, Authorization Bearer (vLLM, LiteLLM,
#   llama.cpp, OpenRouter, DeepSeek, Gemini, etc.)
# - "anthropic": POST /v1/messages con x-api-key + anthropic-version (API nativa)
PROVIDERS: dict[str, dict] = {
    "openai": {
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "env_var": "OPENAI_API_KEY",
        "description": "OpenAI API oficial",
    },
    "anthropic": {
        "api_format": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "env_var": "ANTHROPIC_API_KEY",
        "description": "Anthropic Claude (API nativa)",
    },
    "openrouter": {
        "api_format": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "env_var": "OPENROUTER_API_KEY",
        "description": "OpenRouter (multi-modelo)",
    },
    "opencode": {
        "api_format": "openai",
        "base_url": "https://opencode.ai/zen/v1",
        "env_var": "OPENCODE_API_KEY",
        "description": "OpenCode Zen (openai-compatible)",
    },
    "opencode-go": {
        "api_format": "openai",
        "base_url": "https://opencode.ai/zen/go/v1",
        "env_var": "OPENCODE_API_KEY",
        "description": "OpenCode Go (modelos abiertos, openai-compatible)",
    },
    "deepseek": {
        "api_format": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "env_var": "DEEPSEEK_API_KEY",
        "description": "DeepSeek",
    },
    "gemini": {
        "api_format": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_var": "GEMINI_API_KEY",
        "description": "Google Gemini (compat OpenAI)",
    },
    "local": {
        "api_format": "openai",
        "base_url": "http://localhost:11434/v1",
        "env_var": "",
        "description": "Local (llama.cpp / Ollama / vLLM, sin key)",
    },
    "custom": {
        "api_format": "openai",
        "base_url": "",
        "env_var": "",
        "description": "Endpoint OpenAI-compatible propio",
    },
}

# Modelos sugeridos por provider (para el wizard de setup)
SUGGESTED_MODELS: dict[str, list[str]] = {
    "openai": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
    "openrouter": ["anthropic/claude-sonnet-4", "openai/gpt-4o", "deepseek/deepseek-chat"],
    "opencode": ["gpt-5.1-codex-mini", "deepseek-v4-pro", "qwen3.7-max"],
    "opencode-go": ["qwen3.7-plus", "deepseek-v4-flash", "kimi-k3"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "local": ["local-model"],
    "custom": [],
}


class ConfigError(Exception):
    """Error de configuración con mensaje claro para el usuario."""


@dataclass
class Profile:
    base_url: str
    model: str
    api_key: str | None = None
    temperature: float = 0.7
    provider: str = "openai"
    extra: dict = field(default_factory=dict)


class Config:
    def __init__(self, profiles: dict[str, Profile], default: Profile, path: Path, user_name: str | None = None):
        self.profiles = profiles
        self.default = default
        self.path = path
        self.user_name = user_name

    def get_profile(self, name: str) -> Profile:
        if name in self.profiles:
            base = self.profiles[name]
        elif name == "default":
            base = self.default
        else:
            raise ConfigError(f"El perfil '{name}' no existe. Perfiles: {', '.join(['default'] + list(self.profiles))}")
        if base.api_key:
            return replace(base, api_key=_expand_env(base.api_key))
        return base

    def profile_names(self) -> list[str]:
        return ["default"] + sorted(self.profiles)


def _expand_env(value: str) -> str:
    def _repl(match: re.Match) -> str:
        var = match.group(1)
        if var not in os.environ:
            raise ConfigError(f"Variable de entorno {var} no está definida (usada en config.toml)")
        return os.environ[var]

    return ENV_PATTERN.sub(_repl, value)


def _apply_defaults(profile: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    merged.update({k: v for k, v in profile.items() if v is not None})
    return merged


def load_config(config_dir: Path | str | None = None) -> Config:
    config_dir = Path(config_dir) if config_dir else Path.home() / ".rinari"
    path = config_dir / "config.toml"
    defaults = dict(DEFAULT_CONFIG["default"])

    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        if "default" in data:
            defaults = _apply_defaults(data["default"], defaults)
    else:
        data = {}

    profiles: dict[str, Profile] = {}
    profile_table = data.get("profile", data)
    for name, raw in profile_table.items():
        if name == "default" or not isinstance(raw, dict):
            continue
        merged = _apply_defaults(raw, defaults)
        profiles[name] = Profile(
            base_url=merged["base_url"],
            model=merged["model"],
            api_key=merged.get("api_key") or None,
            temperature=float(merged.get("temperature", 0.7)),
            provider=merged.get("provider") or "openai",
        )

    default_profile = Profile(
        base_url=defaults["base_url"],
        model=defaults["model"],
        api_key=defaults.get("api_key") or None,
        temperature=float(defaults.get("temperature", 0.7)),
        provider=defaults.get("provider") or "openai",
    )
    user_name = None
    if isinstance(data.get("user"), dict):
        user_name = data["user"].get("name") or None
    return Config(profiles=profiles, default=default_profile, path=path, user_name=user_name)


def set_user_name(config_dir: Path | str, name: str) -> Path:
    """Guarda el nombre del usuario en [user] del config.toml."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    cfg = load_config(config_dir)
    lines = [f'[user]\nname = "{name}"\n']
    lines.append("[default]")
    d = cfg.default
    lines.append(f'base_url = "{d.base_url}"')
    lines.append(f'model = "{d.model}"')
    if d.api_key:
        lines.append(f'api_key = "{d.api_key}"')
    if d.provider and d.provider != "openai":
        lines.append(f'provider = "{d.provider}"')
    lines.append(f"temperature = {d.temperature}")
    for pname, p in sorted(cfg.profiles.items()):
        lines.append(f"\n[profile.{pname}]")
        lines.append(f'base_url = "{p.base_url}"')
        lines.append(f'model = "{p.model}"')
        if p.api_key:
            lines.append(f'api_key = "{p.api_key}"')
        if p.provider and p.provider != "openai":
            lines.append(f'provider = "{p.provider}"')
        lines.append(f"temperature = {p.temperature}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_config(config_dir: Path | str, profiles: dict[str, Profile]) -> Path:
    """Escribe perfiles a config.toml (solo los campos base)."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    lines = ['# rinari config — perfiles de endpoints OpenAI-compatibles\n']
    for name, p in sorted(profiles.items()):
        lines.append(f'\n[profile.{name}]')
        lines.append(f'base_url = "{p.base_url}"')
        lines.append(f'model = "{p.model}"')
        if p.api_key:
            lines.append(f'api_key = "{p.api_key}"')
        lines.append(f"temperature = {p.temperature}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def set_profile_model(
    config_dir: Path | str,
    profile_name: str,
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
) -> Path:
    """Cambia el modelo de un perfil (creándolo si hace falta) y guarda.

    Relee el config, modifica el perfil indicado (o [default] si el nombre
    es 'default'), y escribe el archivo. Devuelve el path escrito.
    """
    config_dir = Path(config_dir)
    cfg = load_config(config_dir)

    if profile_name == "default":
        if base_url is not None:
            new_default = Profile(
                base_url=base_url, model=model,
                api_key=api_key if api_key is not None else cfg.default.api_key,
                temperature=cfg.default.temperature,
                provider=provider or cfg.default.provider,
            )
        else:
            new_default = replace(cfg.default, model=model)
        # default se guarda como sección [default] + perfiles aparte
        profiles = dict(cfg.profiles)
        d = new_default
    else:
        profiles = dict(cfg.profiles)
        if profile_name not in profiles:
            if base_url is None:
                raise ConfigError(
                    f"El perfil '{profile_name}' no existe. Usa `rinari setup` para crearlo, "
                    f"o pasa --base-url. Perfiles: {', '.join(cfg.profile_names())}"
                )
            profiles[profile_name] = Profile(
                base_url=base_url, model=model,
                api_key=api_key,
                temperature=cfg.default.temperature,
                provider=provider or "openai",
            )
        else:
            cur = profiles[profile_name]
            new_key = api_key if api_key is not None else cur.api_key
            profiles[profile_name] = (
                Profile(base_url=base_url, model=model, api_key=new_key,
                        temperature=cur.temperature, provider=provider or cur.provider)
                if base_url is not None else replace(cur, model=model)
            )
        d = cfg.default

    # reconstruir el archivo completo: [user] + default + perfiles
    lines = ['# rinari config — perfiles de endpoints OpenAI-compatibles\n']
    if cfg.user_name:
        lines.append(f'[user]\nname = "{cfg.user_name}"\n')
    lines.append('[default]')
    lines.append(f'base_url = "{d.base_url}"')
    lines.append(f'model = "{d.model}"')
    if d.api_key:
        lines.append(f'api_key = "{d.api_key}"')
    if d.provider and d.provider != "openai":
        lines.append(f'provider = "{d.provider}"')
    lines.append(f"temperature = {d.temperature}")
    for name, p in sorted(profiles.items()):
        lines.append(f'\n[profile.{name}]')
        lines.append(f'base_url = "{p.base_url}"')
        lines.append(f'model = "{p.model}"')
        if p.api_key:
            lines.append(f'api_key = "{p.api_key}"')
        if p.provider and p.provider != "openai":
            lines.append(f'provider = "{p.provider}"')
        lines.append(f"temperature = {p.temperature}")
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
