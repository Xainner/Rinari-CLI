"""Curated provider catalog for the interactive model picker (hermes model-style).

These are onboarding presets, not new adapter types: every entry maps to one of
the built-in provider families (openai / anthropic / custom), pre-filling the
base URL and a suggested env-var name. Users can still add any OpenAI-compatible
endpoint manually via the 'custom endpoint' preset or `rinari providers add
custom --endpoint <url>`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    name: str
    provider_type: str  # one of: openai, anthropic, custom
    base_url: str | None = None
    default_env: str | None = None
    local: bool = False  # no auth (local servers)


PROVIDER_CATALOG: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "openai",
        "OpenAI",
        "openai",
        base_url="https://api.openai.com/v1",
        default_env="OPENAI_API_KEY",
    ),
    ProviderPreset("anthropic", "Anthropic", "anthropic", default_env="ANTHROPIC_API_KEY"),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "custom",
        base_url="https://openrouter.ai/api/v1",
        default_env="OPENROUTER_API_KEY",
    ),
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        "custom",
        base_url="https://api.deepseek.com/v1",
        default_env="DEEPSEEK_API_KEY",
    ),
    ProviderPreset(
        "groq",
        "Groq",
        "custom",
        base_url="https://api.groq.com/openai/v1",
        default_env="GROQ_API_KEY",
    ),
    ProviderPreset(
        "together",
        "Together AI",
        "custom",
        base_url="https://api.together.xyz/v1",
        default_env="TOGETHER_API_KEY",
    ),
    ProviderPreset(
        "mistral",
        "Mistral",
        "custom",
        base_url="https://api.mistral.ai/v1",
        default_env="MISTRAL_API_KEY",
    ),
    ProviderPreset(
        "xai", "xAI (Grok)", "custom", base_url="https://api.x.ai/v1", default_env="XAI_API_KEY"
    ),
    ProviderPreset(
        "ollama", "Ollama (local)", "custom", base_url="http://localhost:11434/v1", local=True
    ),
    ProviderPreset(
        "lmstudio", "LM Studio (local)", "custom", base_url="http://localhost:1234/v1", local=True
    ),
    ProviderPreset("custom", "Custom endpoint", "custom"),
)
