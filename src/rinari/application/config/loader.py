"""Config layering: built-in defaults -> user -> selected profile -> project.

Lower layers are listed first; higher layers override. Locked security
keys (future) would be enforced here against lower layers.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from rinari.application.config.profiles import load_profile
from rinari.application.config.schema import Config
from rinari.shared.errors import ConfigurationError
from rinari.shared.paths import HomeLayout

LAYER_DEFAULTS = "defaults"
LAYER_USER = "user"
LAYER_PROJECT = "project"


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    name: str
    data: dict[str, Any]


class EffectiveConfig:
    def __init__(self, layers: list[ConfigLayer]) -> None:
        """Layers in ascending precedence (first lowest)."""
        self.layers = layers
        merged: dict[str, Any] = {}
        for layer in layers:
            _deep_merge(merged, layer.data)
        self.config = Config.from_dict(merged)
        self._merged = merged

    def raw(self) -> dict[str, Any]:
        return dict(self._merged)

    def value(self, dotted: str) -> Any | None:
        return self.config.value(dotted)

    def sources_for(self, dotted: str) -> list[tuple[str, Any]]:
        """(layer_name, layer_value) pairs that define the dotted key."""
        result: list[tuple[str, Any]] = []
        for layer in self.layers:
            value = _walk(layer.data, dotted)
            if value is not None:
                result.append((layer.name, value))
        return result

    def active_profile_name(self) -> str:
        return self.config.profile


def load_defaults() -> dict[str, Any]:
    text = files("rinari").joinpath("assets/default-config.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def load_user_config(layout: HomeLayout) -> dict[str, Any]:
    path = layout.config_file
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"User config {path} is not valid TOML: {exc}") from exc


def load_effective_config(
    layout: HomeLayout,
    project_config: Path | None = None,
    user_data: dict[str, Any] | None = None,
) -> EffectiveConfig:
    layers: list[ConfigLayer] = [ConfigLayer(name=LAYER_DEFAULTS, data=load_defaults())]

    user = user_data if user_data is not None else load_user_config(layout)
    if user:
        layers.append(ConfigLayer(name=LAYER_USER, data=user))

    base = {**load_defaults(), **user}
    profile_name = base.get("profile", "workspace")
    if isinstance(profile_name, dict):
        # Legacy layout: [profile.<name>] tables inline in config.toml.
        legacy = [name for name in profile_name if isinstance(profile_name[name], dict)]
        targets = ", ".join(f"~/.rinari/profiles/{n}.toml" for n in legacy) or "(none found)"
        raise ConfigurationError(
            "The 'profile' key in config.toml must be a string profile name "
            f"(found a table with inline profile sections: {', '.join(legacy) or 'empty'}).",
            hint=(
                f"Legacy inline profiles: move each [profile.<name>] table to {targets}; "
                'then select one with `profile = "<name>"` or '
                "`rinari config set profile <name>`."
            ),
        )
    if not isinstance(profile_name, str) or not profile_name:
        raise ConfigurationError(
            "The 'profile' key in config.toml must be a string profile name "
            f"(found {type(profile_name).__name__}).",
            hint='Use `profile = "<name>"` in config.toml.',
        )
    profile_data = load_profile(profile_name, layout.dir("profiles"))
    if profile_data:
        layers.append(ConfigLayer(name=f"profile:{profile_name}", data=profile_data))

    if project_config is not None and project_config.is_file():
        try:
            data = tomllib.loads(project_config.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Project config {project_config} is not valid TOML: {exc}"
            ) from exc
        if data:
            layers.append(ConfigLayer(name=LAYER_PROJECT, data=data))

    return EffectiveConfig(layers)


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _walk(data: dict[str, Any], dotted: str) -> Any | None:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
