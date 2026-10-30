"""Custom Soul storage (Soul 3.0 store; bundled default Soul 4.0).

Each soul is a directory `<home>/souls/<id>/` with `soul.toml` (id, name,
version, description) and `identity.md` (the injected persona text).
The bundled default ships inside package assets and is never mutated;
user souls live only in the home directory, so a new bundled version never
overwrites an explicit custom Soul. Activation is a single `active_soul`
pointer file (sessions can pin their own). The character intensity
(minimal | balanced | full) lives in `<home>/soul_settings.toml` and applies
to whichever Soul is in effect.

Soul is voice only. It cannot change tool permissions, approvals, security
policy, secret handling, verification truth, or execution state: those are
code-enforced outside the prompt.
"""

from __future__ import annotations

import contextlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError
from rinari.soul.intensity import DEFAULT_INTENSITY, INTENSITIES, validate_intensity

SOURCE_BUNDLED = "bundled"
SOURCE_CUSTOM = "custom"

DEFAULT_SOUL_ID = "rinari-default"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_IDENTITY_CHARS = 65_536


@dataclass(frozen=True, slots=True)
class SoulDefinition:
    id: str
    name: str
    version: str
    description: str
    source: str
    identity: str


def validate_soul_id(soul_id: Any) -> str:
    if not isinstance(soul_id, str) or not _ID_RE.match(soul_id):
        raise InvalidUsageError(
            f"Invalid soul id: {soul_id!r}. Use 1-64 chars: a-z, 0-9, '-'.",
        )
    return soul_id


def validate_identity(identity: Any) -> str:
    # Empty is allowed (plan 0.4.0 #87): a soul without text speaks only with
    # the personality level, and the editor says so.
    if not isinstance(identity, str):
        raise InvalidUsageError("Soul identity text must be a string.")
    if "\0" in identity:
        raise InvalidUsageError("Soul identity text must not contain NUL bytes.")
    if len(identity) > _MAX_IDENTITY_CHARS:
        raise InvalidUsageError(
            f"Soul identity exceeds {_MAX_IDENTITY_CHARS} chars.",
        )
    _validate_unicode(identity)
    return identity


def _validate_unicode(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidUsageError(
            "Soul text contains invalid Unicode. "
            "Paste the original text again or import UTF-8 text."
        ) from exc


def _read_definition(soul_id: str, directory: Path, source: str) -> SoulDefinition | None:
    meta_path = directory / "soul.toml"
    identity_path = directory / "identity.md"
    if not meta_path.is_file() or not identity_path.is_file():
        return None
    try:
        meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
        identity = identity_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    version = meta.get("version")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        return None
    if not isinstance(version, str) or not version.strip() or len(version) > 32:
        return None
    description = meta.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        return None
    try:
        validate_identity(identity)
    except InvalidUsageError:
        return None
    return SoulDefinition(
        id=soul_id,
        name=name.strip(),
        version=version.strip(),
        description=description.strip(),
        source=source,
        identity=identity,
    )


class SoulStore:
    """Bundled defaults (read-only package assets) + custom souls in home."""

    def __init__(self, home: Path) -> None:
        self._dir = Path(home) / "souls"
        self._active_file = Path(home) / "active_soul"
        self._settings_file = Path(home) / "soul_settings.toml"

    def list(self) -> list[SoulDefinition]:
        souls: dict[str, SoulDefinition] = {}
        for soul_id, directory in self._bundled_dirs().items():
            definition = _read_definition(soul_id, directory, SOURCE_BUNDLED)
            if definition is not None:
                souls[soul_id] = definition
        if self._dir.is_dir():
            for entry in sorted(self._dir.iterdir(), key=lambda p: p.name):
                if not entry.is_dir():
                    continue
                try:
                    validate_soul_id(entry.name)
                except InvalidUsageError:
                    continue
                definition = _read_definition(entry.name, entry, SOURCE_CUSTOM)
                if definition is not None:
                    souls[entry.name] = definition
        return [souls[key] for key in sorted(souls)]

    def get(self, soul_id: str) -> SoulDefinition:
        validate_soul_id(soul_id)
        for definition in self.list():
            if definition.id == soul_id:
                return definition
        raise NotFoundError(f"Unknown soul: {soul_id}")

    def create(
        self,
        soul_id: str,
        *,
        name: str,
        identity: str,
        description: str = "",
        version: str = "1.0",
    ) -> SoulDefinition:
        validate_soul_id(soul_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise InvalidUsageError("Soul name must be 1-80 chars.")
        if not isinstance(version, str) or not version.strip() or len(version) > 32:
            raise InvalidUsageError("Soul version must be 1-32 chars.")
        if not isinstance(description, str) or len(description) > 500:
            raise InvalidUsageError("Soul description must be at most 500 chars.")
        validate_identity(identity)
        for value in (name, version, description):
            _validate_unicode(value)
        if any(d.id == soul_id for d in self.list()):
            raise ConflictError(f"Soul already exists: {soul_id}")
        directory = self._dir / soul_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "soul.toml").write_text(
            tomli_w.dumps(
                {
                    "id": soul_id,
                    "name": name.strip(),
                    "version": version.strip(),
                    "description": description.strip(),
                }
            ),
            encoding="utf-8",
        )
        (directory / "identity.md").write_text(identity, encoding="utf-8")
        return self.get(soul_id)

    def update(
        self,
        soul_id: str,
        *,
        name: str | None = None,
        identity: str | None = None,
        description: str | None = None,
        version: str | None = None,
    ) -> SoulDefinition:
        current = self.get(soul_id)
        if current.source != SOURCE_CUSTOM:
            raise InvalidUsageError(
                f"Bundled soul {soul_id!r} is read-only; create a custom soul instead."
            )
        if name is None and identity is None and description is None and version is None:
            raise InvalidUsageError("Nothing to update.")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 80):
            raise InvalidUsageError("Soul name must be 1-80 chars.")
        if version is not None and (
            not isinstance(version, str) or not version.strip() or len(version) > 32
        ):
            raise InvalidUsageError("Soul version must be 1-32 chars.")
        if description is not None and (not isinstance(description, str) or len(description) > 500):
            raise InvalidUsageError("Soul description must be at most 500 chars.")
        if identity is not None:
            validate_identity(identity)
        for value in (name, version, description):
            if value is not None:
                _validate_unicode(value)
        directory = self._dir / soul_id
        (directory / "soul.toml").write_text(
            tomli_w.dumps(
                {
                    "id": soul_id,
                    "name": name.strip() if name is not None else current.name,
                    "version": version.strip() if version is not None else current.version,
                    "description": (
                        description.strip() if description is not None else current.description
                    ),
                }
            ),
            encoding="utf-8",
        )
        if identity is not None:
            (directory / "identity.md").write_text(identity, encoding="utf-8")
        return self.get(soul_id)

    def remove(self, soul_id: str) -> bool:
        current = self.get(soul_id)
        if current.source != SOURCE_CUSTOM:
            raise InvalidUsageError(f"Bundled soul {soul_id!r} cannot be removed.")
        directory = self._dir / soul_id
        (directory / "identity.md").unlink(missing_ok=True)
        (directory / "soul.toml").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()
        if self.active_id() == soul_id:
            self._active_file.unlink(missing_ok=True)
        return True

    def active_id(self) -> str | None:
        if not self._active_file.is_file():
            return None
        try:
            value = self._active_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not value:
            return None
        try:
            validate_soul_id(value)
        except InvalidUsageError:
            return None
        return value

    def activate(self, soul_id: str) -> SoulDefinition:
        definition = self.get(soul_id)
        self._active_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_file.write_text(soul_id + "\n", encoding="utf-8")
        return definition

    # -- character intensity --------------------------------------------------

    def character_intensity(self) -> str:
        """Configured intensity; the default when unset or unreadable."""
        if not self._settings_file.is_file():
            return DEFAULT_INTENSITY
        try:
            data = tomllib.loads(self._settings_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return DEFAULT_INTENSITY
        value = data.get("character_intensity") if isinstance(data, dict) else None
        return value if value in INTENSITIES else DEFAULT_INTENSITY

    def set_character_intensity(self, level: Any) -> str:
        try:
            level = validate_intensity(level)
        except ValueError as exc:
            raise InvalidUsageError(str(exc)) from exc
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings_file.write_text(
            tomli_w.dumps({"character_intensity": level}), encoding="utf-8"
        )
        return level

    # -- sharing (plan 0.4.0 #87) ------------------------------------------------

    def free_id(self, wanted: str) -> str:
        """`wanted`, or `wanted-2`, `wanted-3`... when a soul already has it."""
        taken = {definition.id for definition in self.list()}
        base = wanted[:60].strip("-") or "alma"
        candidate, number = base, 2
        while candidate in taken:
            candidate = f"{base}-{number}"
            number += 1
        return candidate

    def export(self, soul_id: str, destination: Path) -> Path:
        """Write a soul as it lives in the home: `<dest>/<id>/soul.toml` and
        `identity.md`. Never over an existing folder: `<id>-2`..."""
        definition = self.get(soul_id)
        destination = Path(destination)
        if not destination.is_dir():
            raise InvalidUsageError(f"Not a folder: {destination}")
        target, number = destination / definition.id, 2
        while target.exists():
            target = destination / f"{definition.id}-{number}"
            number += 1
        target.mkdir()
        (target / "soul.toml").write_text(
            tomli_w.dumps(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "version": definition.version,
                    "description": definition.description,
                }
            ),
            encoding="utf-8",
        )
        (target / "identity.md").write_text(definition.identity, encoding="utf-8")
        return target

    def import_files(self, paths: list[Path]) -> SoulDefinition:
        """A soul from what the owner picked: its folder, or its `.toml` and
        its `.md` (an `identity.md` or `<name>.md` next to the `.toml` is
        found by itself). It is saved as a new custom soul; an id in use gets
        a suffix, so nothing is overwritten."""
        files = [Path(path) for path in paths]
        if len(files) == 1 and files[0].is_dir():
            folder = files[0]
            files = [folder / "soul.toml", folder / "identity.md"]
        meta_path = next((path for path in files if path.suffix.lower() == ".toml"), None)
        identity_path = next((path for path in files if path.suffix.lower() == ".md"), None)
        if meta_path is None:
            raise InvalidUsageError("Pick the soul's .toml (and its .md), or its folder.")
        if identity_path is None:
            for sibling in (meta_path.with_name("identity.md"), meta_path.with_suffix(".md")):
                if sibling.is_file():
                    identity_path = sibling
                    break
        for path in (meta_path, identity_path):
            if path is not None and (not path.is_file() or path.stat().st_size > 256_000):
                raise InvalidUsageError(f"Cannot read {path.name}.")
        try:
            meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
            identity = identity_path.read_text(encoding="utf-8") if identity_path else ""
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise InvalidUsageError(f"Not a soul file: {exc}") from exc
        name = meta.get("name") if isinstance(meta, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise InvalidUsageError("The .toml has no soul name.")
        wanted = meta.get("id") if isinstance(meta.get("id"), str) else ""
        if not _ID_RE.match(wanted or ""):
            from rinari.profiles.bundles import slug_for

            wanted = slug_for(name) or "alma"
        description = meta.get("description", "")
        version = meta.get("version", "1.0")
        return self.create(
            self.free_id(wanted),
            name=name.strip()[:80],
            identity=identity.replace("\r\n", "\n"),
            description=description if isinstance(description, str) else "",
            version=version if isinstance(version, str) and version.strip() else "1.0",
        )

    @staticmethod
    def _bundled_dirs() -> dict[str, Path]:
        from importlib.resources import files

        base = Path(str(files("rinari").joinpath("assets/souls")))
        if not base.is_dir():
            return {}
        return {entry.name: entry for entry in base.iterdir() if entry.is_dir()}
