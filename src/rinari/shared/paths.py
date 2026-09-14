"""Resolution of the Rinari home directory and its standard layout.

$HOME itself is never an implicit workspace; only the dedicated
config dir under it is Rinari state.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ENV_HOME = "RINARI_HOME"

#: Archivo con el identificador estable del home (namespacea las credenciales
#: del sistema para que un home no pueda tocar las de otro).
HOME_ID_FILE = "home-id"


def _read_home_id(path: Path, *, attempts: int = 1) -> str | None:
    """Lee el identificador; tolera la ventana en que otro proceso lo publica."""
    for attempt in range(attempts):
        with contextlib.suppress(OSError):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        if attempt + 1 < attempts:
            time.sleep(0.01)
    return None


def _fallback_identifier(root: Path) -> str:
    """Identifier determinista cuando el archivo no puede persistirse."""
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def home_identifier(root: Path) -> str:
    """Identificador estable del home; se publica de forma atómica.

    Dos procesos que arrancan a la vez deben quedarse con el *mismo* id: se
    crea con O_EXCL (uno gana, el resto relee el valor ganador) y nunca se
    devuelve un id que no haya quedado persistido; si el home es de solo
    lectura se usa un hash determinista de la ruta.
    """
    path = Path(root) / HOME_ID_FILE
    existing = _read_home_id(path)
    if existing:
        return existing

    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(3):
        candidate = uuid4().hex[:16]
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Otro proceso acaba de ganar: releer (con reintentos) su valor,
            # contemplando la ventana en que todavía no escribió el contenido.
            return _read_home_id(path, attempts=20) or _fallback_identifier(root)
        except OSError:
            return _fallback_identifier(root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(candidate + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return _fallback_identifier(root)
        return _read_home_id(path, attempts=20) or _fallback_identifier(root)

    return _read_home_id(path, attempts=20) or _fallback_identifier(root)


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
    def credentials_lock(self) -> Path:
        """Lock entre procesos para mutaciones del vault de credenciales."""
        return self.root / "credentials.lock"

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
