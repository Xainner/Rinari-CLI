"""Resolution of the Rinari home directory and its standard layout.

$HOME itself is never an implicit workspace; only the dedicated
config dir under it is Rinari state.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ENV_HOME = "RINARI_HOME"

#: Archivo con el identificador estable del home (namespacea las credenciales
#: del sistema para que un home no pueda tocar las de otro).
HOME_ID_FILE = "home-id"


def home_identifier(root: Path) -> str:
    """Identificador estable del home; se persiste en el primer uso.

    Si el home es de solo lectura, se usa un hash determinista de la ruta (el
    scope sigue siendo estable aunque no se pueda escribir el archivo).
    """
    path = Path(root) / HOME_ID_FILE
    with contextlib.suppress(OSError):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = uuid4().hex[:16]
    with contextlib.suppress(OSError):
        path.write_text(value + "\n", encoding="utf-8")
        return value
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


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
