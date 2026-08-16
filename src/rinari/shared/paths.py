"""Resolution of the Rinari home directory and its standard layout.

$HOME itself is never an implicit workspace; only the dedicated
config dir under it is Rinari state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "RINARI_HOME"

_STANDARD_DIRS = (
    "profiles",
    "policies",
    "skills",
    "agents",
    "plugins",
    "memory",
    "sessions",
    "artifacts",
    "cache",
    "logs",
    "credentials",
)


@dataclass(frozen=True, slots=True)
class HomeLayout:
    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def soul_file(self) -> Path:
        return self.root / "soul.md"

    @property
    def constitution_file(self) -> Path:
        return self.root / "constitution.md"

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"

    @property
    def credentials_dir(self) -> Path:
        return self.root / "credentials"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def dir(self, name: str) -> Path:
        return self.root / name


def resolve_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    base = explicit if explicit is not None else os.environ.get(ENV_HOME)
    if base:
        return Path(base).expanduser()
    return Path.home() / ".rinari"


def layout_for(root: str | os.PathLike[str]) -> HomeLayout:
    return HomeLayout(root=Path(root).expanduser().resolve())


def ensure_layout(root: Path) -> HomeLayout:
    root.mkdir(parents=True, exist_ok=True)
    for name in _STANDARD_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return HomeLayout(root=root)
