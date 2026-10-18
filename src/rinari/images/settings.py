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
KINDS = ("chatgpt", "openai", "fal", "google", "openrouter", "comfyui", "mcp")
#: Kinds that use an API key of their own (kept as a credential reference).
KEYED = ("openai", "fal", "google", "openrouter")
OPERATIONS = ("generate", "edit", "variations", "upscale", "remove_background")
_PRIVATE_HTTP = re.compile(
    r"^http://(127\.0\.0\.1|localhost|\[::1\]|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[a-z0-9-]+(\.local|\.lan)?)(:\d+)?(/.*)?$",
    re.IGNORECASE,
)
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
        if kind == "chatgpt" or (kind == "openrouter" and item.get("provider_id")):
            provider = item.get("provider_id")
            if not isinstance(provider, str) or not provider:
                raise ValueError(f"A {kind} connector needs provider_id")
            entry["provider_id"] = provider
        if kind in KEYED:
            ref = item.get("api_key_ref")
            if ref is not None and (not isinstance(ref, str) or "://" not in ref):
                raise ValueError("api_key_ref must be a credential reference")
            entry["api_key_ref"] = ref
        endpoint = item.get("endpoint")
        if endpoint is not None:
            # A local ComfyUI is plain http on this PC or the LAN; the rest are
            # https (or loopback, for a test service).
            local = isinstance(endpoint, str) and bool(_PRIVATE_HTTP.match(endpoint))
            secure = isinstance(endpoint, str) and endpoint.startswith("https://")
            if not (secure or local):
                raise ValueError("endpoint must be https, or http on this PC or the local network")
            entry["endpoint"] = endpoint.rstrip("/")
        if kind == "fal" and item.get("models") is not None:
            models = item["models"]
            if not isinstance(models, dict) or any(
                op not in OPERATIONS or not isinstance(value, str) or len(value) > 120
                for op, value in models.items()
            ):
                raise ValueError("models must map operations to FAL model ids")
            entry["models"] = {op: value for op, value in models.items() if value}
        if kind == "comfyui":
            workflows = item.get("workflows") or {}
            if not isinstance(workflows, dict) or any(
                op not in OPERATIONS or not isinstance(graph, dict)
                for op, graph in workflows.items()
            ):
                raise ValueError("workflows must map operations to ComfyUI workflows (API format)")
            entry["workflows"] = workflows
            entry.setdefault("endpoint", "http://127.0.0.1:8188")
        if kind == "mcp":
            server = item.get("server")
            if not isinstance(server, str) or not server:
                raise ValueError("An MCP connector needs server")
            tools = item.get("tools") or {}
            if not isinstance(tools, dict) or not tools:
                raise ValueError("An MCP connector maps at least one operation to a tool")
            clean_tools: dict[str, dict[str, str]] = {}
            for op, spec in tools.items():
                if (
                    op not in OPERATIONS
                    or not isinstance(spec, dict)
                    or not isinstance(spec.get("tool"), str)
                    or not spec["tool"]
                ):
                    raise ValueError(f"Invalid MCP tool for {op!r}")
                clean_tools[op] = {
                    key: str(spec[key])
                    for key in ("tool", "prompt_arg", "image_arg")
                    if spec.get(key)
                }
            entry["server"] = server
            entry["tools"] = clean_tools
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
