"""Shared, destination-scoped metadata for routing, context and presentation.

The bundled public catalog is advisory. Saved overrides remain separate and
never get overwritten by discovery. No credentials are sent to models.dev.
"""

import json
from functools import lru_cache
from pathlib import Path

from rinari.providers.catalog import OPENCODE_RESPONSES_MODELS, product_for


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("model_metadata.json").read_text(encoding="utf-8"))


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
    metadata["transport"] = transport
    if transport == "anthropic":
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
    elif product not in ("custom", "chatgpt", "github-copilot") and transport == "chat":
        # Advertising thought generation is not evidence of the OpenAI effort dialect.
        metadata.update(reasoning_effort=False, reasoning_levels=[])
    metadata["route_supported"] = transport in ("chat", "responses", "anthropic")
    return metadata


def effective_metadata(provider, model):
    return {
        **model_metadata(provider, model.provider_model_id),
        **(model.settings or {}).get("discovered_capabilities", {}),
        **(model.capabilities or {}),
    }
