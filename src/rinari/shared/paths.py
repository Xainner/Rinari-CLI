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

from rinari.shared.errors import HomeIdUnavailableError, LockTimeoutError
from rinari.shared.locking import file_lock

ENV_HOME = "RINARI_HOME"

#: Archivo con el identificador estable del home (namespacea las credenciales
#: del sistema para que un home no pueda tocar las de otro).
HOME_ID_FILE = "home-id"

#: Lock que serializa la publicación del identificador entre procesos.
HOME_ID_LOCK_FILE = "home-id.lock"

#: Segundos de espera si otro proceso está publicando el identificador.
HOME_ID_LOCK_TIMEOUT = 5.0


def _read_home_id(path: Path) -> str | None:
    """Lee el identificador publicado, si ya existe y está completo."""
    with contextlib.suppress(OSError):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def _fallback_identifier(root: Path) -> str:
    """Identifier determinista cuando el archivo no puede persistirse."""
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def _publish_home_id(root: Path, path: Path, value: str) -> bool:
    """Publica el identificador completo con un reemplazo atómico.

    Nunca se ve el archivo a medio escribir: o no existe, o tiene el id entero.
    Un creador interrumpido deja a lo sumo un temporal, que se limpia.
    """
    temporary = root / f"{HOME_ID_FILE}.{uuid4().hex}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return False


def home_identifier(root: Path) -> str:
    """Identificador estable del home, publicado una sola vez y completo.

    La creación se serializa con un lock y el contenido se publica con un
    reemplazo atómico, así que ningún proceso puede observar un id distinto
    para el mismo home: quien llega segundo espera y lee el valor ganador.
    Si la espera vence, se propaga un error reintentable en vez de inventar un
    identificador alternativo; sólo si el home no admite escritura se usa un
    hash determinista de la ruta.
    """
    root = Path(root)
    path = root / HOME_ID_FILE
    existing = _read_home_id(path)
    if existing:
        return existing

    with contextlib.suppress(OSError):
        root.mkdir(parents=True, exist_ok=True)

    try:
        with file_lock(root / HOME_ID_LOCK_FILE, timeout=HOME_ID_LOCK_TIMEOUT):
            # Dentro del lock: otro pudo publicarlo mientras esperábamos.
            existing = _read_home_id(path)
            if existing:
                return existing
            candidate = uuid4().hex[:16]
            if not _publish_home_id(root, path, candidate):
                return _fallback_identifier(root)
            return _read_home_id(path) or _fallback_identifier(root)
    except LockTimeoutError:
        raise HomeIdUnavailableError(
            f"Another process is publishing {HOME_ID_FILE} for {root}",
            hint="Retry in a moment; the identifier must be the same for every process.",
        ) from None
    except OSError:
        # Home sin permiso de escritura: identificador determinista y estable.
        return _fallback_identifier(root)


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
