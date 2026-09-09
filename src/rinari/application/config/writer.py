"""Read-modify-write of the user config file (~/.rinari/config.toml).

Only user-owned overrides live here; defaults stay in the packaged asset.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from rinari.application.config.schema import Config, key_type
from rinari.shared.errors import ConfigurationError
from rinari.shared.paths import HomeLayout


def read_user_data(layout: HomeLayout) -> dict[str, Any]:
    path = layout.config_file
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"User config {path} is not valid TOML: {exc}") from exc


def write_user_data(layout: HomeLayout, data: dict[str, Any]) -> Path:
    Config.from_dict(_with_defaults(data))
    path = layout.config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(_ordered(data)), encoding="utf-8")
    return path


def _with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    from rinari.application.config.loader import load_defaults

    base = load_defaults()
    _deep(base, user)
    return base


def _ordered(data: dict[str, Any]) -> dict[str, Any]:
    """Order keys: plain scalars first (schema order), then tables."""
    scalars = ["user_name", "language", "model", "profile"]
    sections = Config().sections()
    ordered: dict[str, Any] = {}
    for key in scalars:
        if key in data:
            ordered[key] = data[key]
    for section in sections:
        if section in data:
            ordered[section] = dict(data[section])
    return ordered


def set_dotted(user: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    updated = dict(user)
    parts = dotted.split(".")
    cursor = updated
    for part in parts[:-1]:
        table = dict(cursor.get(part) or {})
        cursor[part] = table
        cursor = table
    cursor[parts[-1]] = value
    return updated


def unset_dotted(user: dict[str, Any], dotted: str) -> tuple[dict[str, Any], bool]:
    updated = dict(user)
    parts = dotted.split(".")
    cursor = updated
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        raw = cursor.get(part)
        if not isinstance(raw, dict):
            return updated, False
        table = dict(raw)
        cursor[part] = table
        parents.append((cursor, part))
        cursor = table
    key = parts[-1]
    if key not in cursor:
        return updated, False
    del cursor[key]
    for parent, part in reversed(parents):
        if parent[part]:
            break
        del parent[part]
    return updated, True


def parse_value(raw: str, key: str) -> Any:
    """Parse a CLI string into the declared type of `key`."""
    declared = key_type(key)
    if declared == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"'{raw}' is not a valid integer for {key}") from exc
    if declared == "bool":
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigurationError(f"'{raw}' is not a valid boolean for {key}")
    if declared == "list[str]":
        return _parse_list(raw)
    return raw


def _parse_list(raw: str) -> list[str]:
    import json as _json

    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = _json.loads(text)
        except _json.JSONDecodeError as exc:
            raise ConfigurationError(f"'{raw}' is not a valid JSON array for a list key") from exc
        if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
            raise ConfigurationError("list key expects a JSON array of strings")
        return parsed
    items = [item.strip() for item in text.split(",")]
    if not items or not all(items):
        raise ConfigurationError("list key expects comma-separated non-empty values")
    return items


def _deep(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep(target[key], value)
        else:
            target[key] = value
