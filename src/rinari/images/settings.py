"""Ajustes → Visión e imágenes → Creación de imágenes (plan 0.4.0 #28).

`~/.rinari/images.json` keeps the connectors and the choices, never a secret:

    {"connectors": [{"id": "codex", "kind": "chatgpt", "label": "ChatGPT (Codex)",
                     "provider_id": "prov_…", "model": ""},
                    {"id": "openai", "kind": "openai", "label": "OpenAI",
                     "api_key_ref": "keyring://media/openai/api-key", "model": "gpt-image-1"}],
     "default_connector": "codex", "edit_connector": null, "session_budget_usd": null}

There is no default connector (owner's decision): without one, the tools say
so honestly instead of improvising.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

FILE = "images.json"
#: Connector kinds this Engine knows; the settings reject any other.
KINDS = ("chatgpt", "openai")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def empty() -> dict[str, Any]:
    return {
        "connectors": [],
        "default_connector": None,
        "edit_connector": None,
        "session_budget_usd": None,
    }


def load(home: Path) -> dict[str, Any]:
    path = Path(home) / FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    try:
        return validate(raw)
    except ValueError:
        # A hand-edited file that no longer validates: start clean, never crash.
        return empty()


def save(home: Path, value: dict[str, Any]) -> dict[str, Any]:
    clean = validate(value)
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=home, prefix=".images-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, ensure_ascii=False, indent=2)
    os.replace(temp, home / FILE)
    return clean


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Image settings must be an object")
    connectors = value.get("connectors", [])
    if not isinstance(connectors, list):
        raise ValueError("connectors must be a list")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in connectors:
        if not isinstance(item, dict):
            raise ValueError("Each connector must be an object")
        cid = item.get("id")
        if not isinstance(cid, str) or not _ID.match(cid) or cid in seen:
            raise ValueError(f"Invalid or repeated connector id: {cid!r}")
        seen.add(cid)
        kind = item.get("kind")
        if kind not in KINDS:
            raise ValueError(f"Unknown connector kind: {kind!r}")
        label = item.get("label") or cid
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError("label must be a short string")
        model = item.get("model") or ""
        if not isinstance(model, str) or len(model) > 120:
            raise ValueError("model must be a short string")
        entry: dict[str, Any] = {"id": cid, "kind": kind, "label": label, "model": model}
        if kind == "chatgpt":
            provider = item.get("provider_id")
            if not isinstance(provider, str) or not provider:
                raise ValueError("A ChatGPT connector needs provider_id")
            entry["provider_id"] = provider
        if kind == "openai":
            ref = item.get("api_key_ref")
            if ref is not None and (not isinstance(ref, str) or "://" not in ref):
                raise ValueError("api_key_ref must be a credential reference")
            entry["api_key_ref"] = ref
            endpoint = item.get("endpoint")
            if endpoint is not None:
                if not isinstance(endpoint, str) or not endpoint.startswith(
                    ("https://", "http://127.0.0.1", "http://localhost")
                ):
                    raise ValueError("endpoint must be https (or a local address)")
                entry["endpoint"] = endpoint.rstrip("/")
        clean.append(entry)
    ids = {item["id"] for item in clean}
    default = value.get("default_connector")
    edit = value.get("edit_connector")
    for name, choice in (("default_connector", default), ("edit_connector", edit)):
        if choice is not None and choice not in ids:
            raise ValueError(f"{name} names no connector: {choice!r}")
    budget = value.get("session_budget_usd")
    if budget is not None and (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or budget < 0
        or budget > 10_000
    ):
        raise ValueError("session_budget_usd must be between 0 and 10000, or null")
    return {
        "connectors": clean,
        "default_connector": default,
        "edit_connector": edit,
        "session_budget_usd": float(budget) if budget is not None else None,
    }
