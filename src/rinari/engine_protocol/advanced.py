"""Ajustes → Avanzado (plan 0.4.0 #96): model execution and the turn's limits.

Execution (how many calls go to a provider at once, how long Rinari waits,
the output cap per model) has its own pair of methods instead of riding
inside the vision settings. The turn's limits are the `config.toml` keys the
runtime reads every turn, each with the value in use, its default and range,
so the app can show what changed and put it back.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rinari.application.config.loader import load_defaults, load_effective_config
from rinari.application.config.schema import _walk
from rinari.application.config.writer import (
    read_user_data,
    set_dotted,
    unset_dotted,
    write_user_data,
)
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.shared.errors import ConfigurationError

# (key, type, minimum, maximum): the ranges are the schema's.
LIMITS: tuple[tuple[str, str, float | None, float | None], ...] = (
    ("runtime.emergency.max_runtime_minutes", "int", 1, 1440),
    ("runtime.emergency.max_model_calls", "int", 1, 100_000),
    ("runtime.emergency.max_tool_calls", "int", 1, 1_000_000),
    ("runtime.emergency.max_subagent_calls", "int", 1, 10_000),
    ("runtime.cost.max_turn_cost", "float", 0, None),
    ("runtime.safeguards.loop_detection", "bool", None, None),
    ("runtime.tools.max_concurrency", "int", 1, 32),
    ("runtime.tools.observation_bytes", "int", 4096, 16_777_216),
    ("runtime.tools.round_observation_bytes", "int", 4096, 67_108_864),
    ("context.artifact_output_threshold_kb", "int", 1, 1024 * 1024),
    ("agents.enabled", "bool", None, None),
    ("agents.max_concurrent", "int", 1, 64),
    ("agents.max_depth", "int", 0, 8),
    ("agents.max_total", "int", 1, 128),
    # Ajustes → Permisos: full access asks before sending to a new destination
    # after external content (plan-permisos-v4, off by default).
    ("permissions.ask_new_destinations", "bool", None, None),
)
LIMIT_KEYS = {key for key, *_ in LIMITS}


def register_advanced(dispatcher: Any, services: Any) -> None:
    dispatcher.register("execution.settings.get", lambda params: execution_get(services))
    dispatcher.register("execution.settings.set", lambda params: execution_set(services, params))
    dispatcher.register("advanced.settings.get", lambda params: limits_get(services))
    dispatcher.register("advanced.settings.set", lambda params: limits_set(services, params))
    dispatcher.register("storage.usage", lambda params: storage_usage(services))


# -- model execution ----------------------------------------------------------------


def execution_get(services: Any) -> dict[str, Any]:
    from rinari.models.execution import DEFAULT_CONCURRENCY, policy
    from rinari.providers.adapters.http import STREAM_DEFAULTS

    return {
        "settings": policy(services.ctx.home),
        "defaults": {"max_concurrency": DEFAULT_CONCURRENCY, "timeouts": dict(STREAM_DEFAULTS)},
    }


def execution_set(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    from rinari.models.execution import validate

    value = params.get("settings")
    if not isinstance(value, dict):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'settings' must be an object.")
    try:
        clean = validate(value)
    except ValueError as exc:
        raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
    _write_json(services.ctx.home, "model-execution.json", clean)
    return execution_get(services)


# -- the turn's limits ----------------------------------------------------------------


def limits_get(services: Any) -> dict[str, Any]:
    defaults = load_defaults()
    current = services.ctx.config.config.to_dict()
    rows = []
    for key, kind, minimum, maximum in LIMITS:
        rows.append(
            {
                "key": key,
                "type": kind,
                "value": _walk(current, key),
                "default": _walk(defaults, key),
                "min": minimum,
                "max": maximum,
            }
        )
    return {"limits": rows}


def limits_set(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    """`values`: {key: value}; `null` puts the default back."""
    values = params.get("values")
    if not isinstance(values, dict) or not values:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'values' must be a non-empty object.")
    unknown = sorted(set(values) - LIMIT_KEYS)
    if unknown:
        raise EngineProtocolError(INVALID_PARAMS, f"Not a limit setting: {', '.join(unknown)}")
    ctx = services.ctx
    data = read_user_data(ctx.layout)
    for key, value in values.items():
        data = unset_dotted(data, key)[0] if value is None else set_dotted(data, key, value)
    try:
        write_user_data(ctx.layout, data)
    except ConfigurationError as exc:
        raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
    # The runtime reads the effective config every turn: the change applies to
    # the next one. A project's own layer stays on top.
    from rinari.application.config.loader import EffectiveConfig

    project = [layer for layer in ctx.config.layers if layer.name == "project"]
    ctx.config = EffectiveConfig([*load_effective_config(ctx.layout).layers, *project])
    return limits_get(services)


# -- storage ------------------------------------------------------------------------

# What each part of ~/.rinari holds, by entry name. Anything else is «other».
STORAGE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("conversations", ("state.db", "state.db-wal", "state.db-shm", "history.sqlite", "sessions")),
    ("workspaces", ("workspaces",)),
    ("artifacts", ("artifacts", "change-blobs")),
    ("browser", ("browser",)),
    ("cache", ("cache", "scratch")),
    ("logs", ("logs",)),
    ("backups", ("backups",)),
)


def storage_usage(services: Any) -> dict[str, Any]:
    """How much each part of Rinari's folder takes (Ajustes → Avanzado)."""
    home = Path(services.ctx.home)
    named = {name: section for section, names in STORAGE_SECTIONS for name in names}
    totals: dict[str, dict[str, int]] = {
        section: {"bytes": 0, "files": 0} for section, _ in STORAGE_SECTIONS
    }
    totals["other"] = {"bytes": 0, "files": 0}
    try:
        entries = list(os.scandir(home))
    except OSError:
        entries = []
    for entry in entries:
        section = named.get(entry.name)
        if section is None and entry.name.startswith(("context-discovery-", "context-window-")):
            section = "cache"
        size, files = _size(entry)
        bucket = totals[section or "other"]
        bucket["bytes"] += size
        bucket["files"] += files
    rows = [{"id": key, **value} for key, value in totals.items()]
    return {"home": str(home), "sections": rows, "total": sum(r["bytes"] for r in rows)}


def _size(entry: os.DirEntry[str]) -> tuple[int, int]:
    try:
        if entry.is_symlink():
            return 0, 0
        if entry.is_file():
            return entry.stat().st_size, 1
        if not entry.is_dir():
            return 0, 0
    except OSError:
        return 0, 0
    size = files = 0
    stack = [entry.path]
    while stack:
        try:
            children = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for child in children:
            try:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    stack.append(child.path)
                elif child.is_file():
                    size += child.stat().st_size
                    files += 1
            except OSError:
                continue
    return size, files


def _write_json(home: Any, name: str, value: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=name.split(".")[0] + "-", suffix=".json", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(tmp, home / name)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
