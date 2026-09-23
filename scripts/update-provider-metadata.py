"""Refresh the advisory model snapshot from models.dev (no account credentials).

Run from the CLI repository with `uv run python scripts/update-provider-metadata.py`.
Review the resulting diff before publishing: transport support stays in metadata.py.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

SOURCE = "https://models.dev/api.json"
PRODUCTS = {
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
FIELDS = ("limit", "modalities", "tool_call", "reasoning", "provider", "interleaved")


def main():
    response = httpx.get(SOURCE, timeout=30, headers={"User-Agent": "Rinari catalog updater/0.1"})
    response.raise_for_status()
    upstream = response.json()
    models = {}
    for product, key in PRODUCTS.items():
        entries = upstream[key]["models"]  # fail closed if upstream renames a product
        models[product] = {
            name: {k: value[k] for k in FIELDS if k in value} for name, value in entries.items()
        }
    snapshot = {"source": SOURCE, "updated_at": datetime.now(UTC).isoformat(), "models": models}
    path = Path(__file__).resolve().parents[1] / "src/rinari/providers/model_metadata.json"
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"Updated {sum(map(len, models.values()))} model entries across {len(models)} products.")


if __name__ == "__main__":
    main()
