"""Shared, destination-scoped metadata for routing, context and presentation.

The public catalog is advisory. Saved overrides remain separate and never get
overwritten by discovery. No credentials are sent to models.dev.

The bundled snapshot ages with the release: a model published after it (Space
Bunny, one day later) fell back to 128k. "Refresh models" downloads the public
list into the home's cache, and the newer of the two is used.
"""

import contextlib
import json
import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from rinari.providers.catalog import OPENCODE_RESPONSES_MODELS, product_for

PUBLIC_CATALOG_URL = "https://models.dev/api.json"
#: Rinari product -> models.dev provider key.
PUBLIC_PRODUCTS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "xai": "xai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "opencode-go": "opencode-go",
    "opencode-zen": "opencode",
    "openrouter": "openrouter",
    "groq": "groq",
    "together": "togetherai",
    "deepinfra": "deepinfra",
    "fireworks": "fireworks-ai",
    "zai": "zai",
    "zai-coding": "zai-coding-plan",
    "moonshot": "moonshotai",
    "kimi-coding": "kimi-code-plan-global",
    "minimax": "minimax",
    "minimax-coding": "minimax-coding-plan",
    "github-copilot": "github-copilot",
}
PUBLIC_FIELDS = ("limit", "modalities", "tool_call", "reasoning", "provider", "interleaved")
_CACHE_NAME = "model_metadata.json"
_cache_dir: Path | None = None


def use_cache_dir(path: Path | None) -> None:
    """Where a downloaded catalog lives (the home's cache directory)."""
    global _cache_dir
    _cache_dir = path
    catalog.cache_clear()


def _bundled() -> dict:
    return json.loads(Path(__file__).with_name(_CACHE_NAME).read_text(encoding="utf-8"))


def _cached() -> dict | None:
    if _cache_dir is None:
        return None
    with contextlib.suppress(OSError, ValueError):
        data = json.loads((_cache_dir / _CACHE_NAME).read_text(encoding="utf-8"))
        if isinstance(data.get("models"), dict) and isinstance(data.get("updated_at"), str):
            return data
    return None


@lru_cache(maxsize=1)
def catalog():
    bundled = _bundled()
    cached = _cached()
    if cached is not None and cached["updated_at"] > bundled["updated_at"]:
        return cached
    return bundled


def public_snapshot(upstream: dict, *, now: datetime | None = None) -> dict:
    """Rinari's snapshot of the models.dev payload (only the fields it uses)."""
    models = {}
    for product, key in PUBLIC_PRODUCTS.items():
        entries = (upstream.get(key) or {}).get("models") or {}
        models[product] = {
            name: {field: value[field] for field in PUBLIC_FIELDS if field in value}
            for name, value in entries.items()
            if isinstance(value, dict)
        }
    if not any(models.values()):
        raise ValueError("the public catalog had none of the known providers")
    stamp = (now or datetime.now(UTC)).isoformat()
    return {"source": PUBLIC_CATALOG_URL, "updated_at": stamp, "models": models}


def refresh_public_catalog(get) -> dict:
    """Download the public list into the cache. ``get`` is an httpx-style get."""
    if _cache_dir is None:
        raise RuntimeError("no cache directory for the model catalog")
    response = get(
        PUBLIC_CATALOG_URL, timeout=20, headers={"User-Agent": "Rinari catalog updater/0.1"}
    )
    response.raise_for_status()
    snapshot = public_snapshot(response.json())
    _cache_dir.mkdir(parents=True, exist_ok=True)
    target = _cache_dir / _CACHE_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    catalog.cache_clear()
    return {
        "updated_at": snapshot["updated_at"],
        "models": sum(len(entries) for entries in snapshot["models"].values()),
    }


def claude_reasoning(model):
    model = model.replace(".", "-")
    modern = (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    )
    if any(model == name or model.startswith(name + "-") for name in modern):
        return "adaptive", ["low", "medium", "high", "xhigh", "max"]
    if model.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
        return "adaptive", ["low", "medium", "high", "max"]
    if model.startswith(
        ("claude-opus-4", "claude-sonnet-4", "claude-haiku-4-5", "claude-3-7-sonnet")
    ):
        return "budget", ["low", "medium", "high"]
    return None, []


def model_metadata(provider, model_id):
    product = product_for(provider)
    snapshot = catalog()
    entry = snapshot["models"].get(product, {}).get(model_id, {})
    limit = entry.get("limit", {})
    metadata = {"source": snapshot["source"], "updated_at": snapshot["updated_at"]} if entry else {}
    for source, target in (
        ("context", "max_context_tokens"),
        ("input", "max_input_tokens"),
        ("output", "max_output_tokens"),
    ):
        if type(limit.get(source)) is int and limit[source] > 0:
            metadata[target] = limit[source]
    if entry:
        metadata.update(
            vision=("image" in entry["modalities"]["input"])
            if isinstance(entry.get("modalities", {}).get("input"), list)
            else None,
            advertised_modalities=entry.get("modalities", {}),
            modalities={
                "input": ["text", "image"]
                if "image" in entry.get("modalities", {}).get("input", [])
                else ["text"],
                "output": ["text"],
            },
            tool_calls=entry.get("tool_call"),
            advertised_reasoning=entry.get("reasoning"),
        )
    transport = "chat"
    if product in ("opencode-go", "opencode-zen"):
        npm = entry.get("provider", {}).get("npm")
        # IDs on /messages per the OpenCode Go endpoint table (updated
        # 2026-09-22); the list backs up the catalog's npm hint.
        if npm == "@ai-sdk/anthropic" or model_id in {
            "minimax-m3",
            "minimax-m2.7",
            "minimax-m2.5",
            "qwen3.8-max",
            "qwen3.8-flash",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.6-plus",
        }:
            transport = "anthropic"
        elif npm == "@ai-sdk/openai" or model_id in OPENCODE_RESPONSES_MODELS:
            transport = "responses"
        elif npm == "@ai-sdk/google":
            transport = "unsupported-google-native"
    if (
        provider.type == "anthropic"
        or (provider.settings or {}).get("protocol") == "anthropic-compatible"
    ):
        transport = "anthropic"
    if product == "chatgpt":
        transport = "responses"
    if product == "github-copilot":
        transport = "unsupported-copilot-route"  # discovery supplies supported_endpoints
    if product == "claude-subscription":
        # The official CLI owns this route: there is no HTTP wire transport to
        # infer, and it validates the effort itself.
        transport = "claude-cli"
    metadata["transport"] = transport
    if transport == "claude-cli":
        from rinari.providers.claude_cli import CLAUDE_EFFORT_LEVELS

        # The five levels `--effort` takes. Rinari offers eight; the CLI
        # discards the rest with a warning nothing surfaces, so claiming them
        # would put choices in the composer that silently do nothing.
        metadata.update(
            reasoning_effort=True,
            reasoning_levels=[
                level
                for level in ("low", "medium", "high", "xhigh", "max")
                if level in CLAUDE_EFFORT_LEVELS
            ],
        )
    elif transport == "anthropic":
        mode, levels = claude_reasoning(model_id)
        metadata.update(reasoning_effort=bool(mode), reasoning_mode=mode, reasoning_levels=levels)
    elif product == "gemini":
        if model_id.startswith("gemini-3"):
            metadata.update(
                reasoning_effort=True,
                reasoning_levels=["low", "medium", "high"]
                if "flash" in model_id
                else ["low", "high"],
            )
        elif model_id.startswith("gemini-2.5"):
            metadata.update(reasoning_effort=True, reasoning_levels=["low", "medium", "high"])
        else:
            metadata.update(reasoning_effort=False, reasoning_levels=[])
    elif product == "openrouter" and entry.get("reasoning") is True:
        metadata.update(
            reasoning_effort=True,
            reasoning_levels=["low", "medium", "high"],
            reasoning_dialect="openrouter",
        )
    elif entry and entry.get("reasoning") is False:
        metadata.update(reasoning_effort=False, reasoning_levels=[])
    elif product in ("openai", "chatgpt", "github-copilot") and entry.get("reasoning") is True:
        metadata.update(reasoning_effort=True, reasoning_levels=["low", "medium", "high"])
    elif (
        product in ("opencode-go", "opencode-zen")
        and transport == "chat"
        and entry.get("reasoning") is True
    ):
        # Checked against OpenCode Go (2026-09-24): deepseek-v4-pro, glm-5.3
        # and kimi-k3 accept `reasoning_effort` on /chat/completions and
        # return reasoning with it.
        metadata.update(reasoning_effort=True, reasoning_levels=["low", "medium", "high"])
    elif product not in ("custom", "chatgpt", "github-copilot") and transport == "chat":
        # Advertising thought generation is not evidence of the OpenAI effort dialect.
        metadata.update(reasoning_effort=False, reasoning_levels=[])
    metadata["route_supported"] = transport in ("chat", "responses", "anthropic", "claude-cli")
    return metadata


def effective_metadata(provider, model):
    return {
        **model_metadata(provider, model.provider_model_id),
        **(model.settings or {}).get("discovered_capabilities", {}),
        **(model.capabilities or {}),
    }
