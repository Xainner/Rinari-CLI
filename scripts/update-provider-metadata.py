"""Refresh the bundled advisory model snapshot from models.dev (no account credentials).

Run from the CLI repository with `uv run python scripts/update-provider-metadata.py`.
Review the resulting diff before publishing: transport support stays in metadata.py.
At runtime, "Refresh models" downloads the same list into the home's cache.
"""

import json
from pathlib import Path

import httpx

from rinari.providers.metadata import PUBLIC_CATALOG_URL, public_snapshot


def main():
    response = httpx.get(
        PUBLIC_CATALOG_URL, timeout=30, headers={"User-Agent": "Rinari catalog updater/0.1"}
    )
    response.raise_for_status()
    snapshot = public_snapshot(response.json())
    path = Path(__file__).resolve().parents[1] / "src/rinari/providers/model_metadata.json"
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    models = snapshot["models"]
    print(f"Updated {sum(map(len, models.values()))} model entries across {len(models)} products.")


if __name__ == "__main__":
    main()
