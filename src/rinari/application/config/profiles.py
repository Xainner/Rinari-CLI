"""Built-in capability profiles and user profile files.

Profiles are config presets that sit above user config in precedence
(docs/commands.md section 26). A profile only overrides the keys it
defines; everything else inherits from lower layers.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from rinari.shared.errors import ConfigurationError

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "safe": {
        "permissions": {"profile": "read-only"},
        "network": {"mode": "ask"},
    },
    "read-only": {
        "permissions": {"profile": "read-only"},
    },
    "workspace": {},
    "full-access": {
        "permissions": {"profile": "full-access"},
    },
}


def load_profile(name: str, profiles_dir: Path) -> dict[str, Any]:
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    path = profiles_dir / f"{name}.toml"
    if not path.is_file():
        raise ConfigurationError(
            f"Unknown profile: {name}",
            hint=f"Available built-in profiles: {', '.join(sorted(BUILTIN_PROFILES))}",
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Profile {name} is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"Profile {name} must define a TOML table at the root")
    return data


def available_profiles(profiles_dir: Path) -> list[str]:
    names = list(BUILTIN_PROFILES)
    if profiles_dir.is_dir():
        names.extend(p.stem for p in sorted(profiles_dir.glob("*.toml")))
    return sorted(set(names))
