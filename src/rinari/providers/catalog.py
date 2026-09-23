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
    auth: str = "api-key"
    experimental: bool = False
    enabled: bool = True


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
        "opencode-zen",
        "OpenCode Zen",
        "custom",
        base_url="https://opencode.ai/zen/v1",
        default_env="OPENCODE_API_KEY",
    ),
    ProviderPreset(
        "opencode-go",
        "OpenCode Go",
        "custom",
        base_url="https://opencode.ai/zen/go/v1",
        default_env="OPENCODE_GO_API_KEY",
    ),
    ProviderPreset(
        "ollama", "Ollama (local)", "custom", base_url="http://localhost:11434/v1", local=True
    ),
    ProviderPreset(
        "lmstudio", "LM Studio (local)", "custom", base_url="http://localhost:1234/v1", local=True
    ),
    ProviderPreset("custom", "Custom endpoint", "custom"),
    ProviderPreset(
        "gemini",
        "Google Gemini",
        "custom",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
    ),
    ProviderPreset(
        "deepinfra",
        "DeepInfra",
        "custom",
        "https://api.deepinfra.com/v1/openai",
        "DEEPINFRA_API_KEY",
    ),
    ProviderPreset(
        "fireworks",
        "Fireworks",
        "custom",
        "https://api.fireworks.ai/inference/v1",
        "FIREWORKS_API_KEY",
    ),
    ProviderPreset("zai", "Z.ai — API", "custom", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    ProviderPreset(
        "zai-coding",
        "Z.ai — Coding Plan",
        "custom",
        "https://api.z.ai/api/coding/paas/v4",
        "ZAI_CODING_API_KEY",
    ),
    ProviderPreset(
        "moonshot", "Moonshot — API", "custom", "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"
    ),
    ProviderPreset(
        "kimi-coding", "Kimi — Coding", "custom", "https://api.kimi.com/coding/v1", "KIMI_API_KEY"
    ),
    ProviderPreset(
        "minimax",
        "MiniMax — API",
        "anthropic",
        "https://api.minimax.io/anthropic/v1",
        "MINIMAX_API_KEY",
    ),
    ProviderPreset(
        "minimax-coding",
        "MiniMax — Token Plan",
        "anthropic",
        "https://api.minimax.io/anthropic/v1",
        "MINIMAX_CODING_API_KEY",
    ),
    ProviderPreset(
        "chatgpt",
        "ChatGPT — subscription, Codex access",
        "custom",
        "https://chatgpt.com/backend-api/codex",
        auth="oauth",
        experimental=True,
    ),
    ProviderPreset(
        "github-copilot",
        "GitHub Copilot",
        "custom",
        "https://api.githubcopilot.com",
        auth="oauth",
        experimental=True,
    ),
)


def product_for(record) -> str:
    """Never identify accounts by alias or by a model name on a custom gateway."""
    endpoint = (
        record.endpoint
        or (
            "https://api.anthropic.com"
            if record.type == "anthropic"
            else "https://api.openai.com/v1"
            if record.type == "openai"
            else ""
        )
    ).rstrip("/")
    explicit = (record.settings or {}).get("product_id")
    if record.type == "custom":
        if endpoint in ("http://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1"):
            endpoint = endpoint.replace("127.0.0.1", "localhost")
        elif endpoint == "https://api.deepseek.com":
            endpoint += "/v1"
    matches = [
        p
        for p in PROVIDER_CATALOG
        if p.key != "custom"
        and p.provider_type == record.type
        and endpoint == (p.base_url or "https://api.anthropic.com").rstrip("/")
    ]
    if explicit and any(p.key == explicit for p in matches):
        return explicit
    if record.type == "anthropic" and endpoint == "https://api.anthropic.com/v1":
        return "anthropic"
    if len(matches) == 1:
        return matches[0].key
    return "custom"


DASHBOARDS = {
    "openai": "https://platform.openai.com/usage",
    "anthropic": "https://platform.claude.com/usage",
    "opencode-go": "https://opencode.ai/console",
    "opencode-zen": "https://opencode.ai/console",
    "openrouter": "https://openrouter.ai/settings/credits",
    "deepseek": "https://platform.deepseek.com/usage",
    "chatgpt": "https://chatgpt.com/codex/settings/usage",
    "github-copilot": "https://github.com/settings/copilot",
    "gemini": "https://aistudio.google.com/",
    "xai": "https://console.x.ai/",
    "groq": "https://console.groq.com/",
    "together": "https://api.together.ai/",
    "deepinfra": "https://deepinfra.com/dash",
    "fireworks": "https://app.fireworks.ai/",
    "mistral": "https://console.mistral.ai/",
    "zai": "https://platform.z.ai/",
    "zai-coding": "https://platform.z.ai/",
    "moonshot": "https://platform.moonshot.ai/",
    "kimi-coding": "https://www.kimi.com/code/console",
    "minimax": "https://platform.minimax.io/",
    "minimax-coding": "https://platform.minimax.io/",
}


def catalog_view():
    return [
        {
            "id": p.key,
            "name": p.name,
            "provider_type": p.provider_type,
            "endpoint": p.base_url or "",
            "auth_methods": ["api-key", "none"]
            if p.key == "custom"
            else ["none" if p.local else p.auth],
            "experimental": p.experimental,
            "enabled": p.enabled,
            "local": p.local,
        }
        for p in PROVIDER_CATALOG
    ]


# Model IDs on OpenCode endpoints that live on the OpenAI Responses API
# (/responses) instead of /chat/completions. Vendor catalog snapshot from
# https://opencode.ai/docs/go/ (per-model endpoint table, re-checked
# 2026-09-13): grok-4.5 moved to /chat/completions and grok-4.6 was added on
# /responses since the previous snapshot; chat calls to responses-only IDs
# fail with a bare HTTP 500. Default transport for matching saved models; an
# explicit per-model setting always wins.
OPENCODE_RESPONSES_MODELS: frozenset[str] = frozenset(
    {
        "grok-4.6",
        "gpt-5.6-luna",
        "muse-spark-1.3-contributor",
        "muse-spark-1.2-contributor",
    }
)
