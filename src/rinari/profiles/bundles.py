"""Rinari Profile bundles (Phase 11).

A profile is a named working-configuration bundle: soul + session mode +
per-agent model assignments. Stored as one TOML file per id under
`<home>/profile_bundles/`. Applying a profile only calls the existing
setters (soul activation, agent config, session mode) and reports exactly
what changed — it never invents policy or touches credentials.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
MAX_DESCRIPTION_CHARS = 500
#: The built-in profile: always present, active unless another is chosen, and
#: where every project and conversation lives until moved. It can be edited
#: (its file is written then) but never removed.
DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "Default"


@dataclass(slots=True)
class ProfileBundle:
    id: str
    name: str
    description: str = ""
    soul_id: str | None = None
    mode: str | None = None
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def builtin(self) -> bool:
        return self.id == DEFAULT_PROFILE_ID

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "soul_id": self.soul_id,
            "mode": self.mode,
            "agents": {k: dict(v) for k, v in self.agents.items()},
            "builtin": self.builtin,
        }


def default_bundle() -> ProfileBundle:
    return ProfileBundle(id=DEFAULT_PROFILE_ID, name=DEFAULT_PROFILE_NAME)


def _validate_bundle(bundle_id: str, name: str, description: str) -> None:
    if not PROFILE_ID_RE.match(bundle_id or ""):
        raise InvalidUsageError("Profile id must match [a-z0-9-], start alnum, max 64 chars.")
    if not (name or "").strip() or len(name) > 80:
        raise InvalidUsageError("Profile name must be 1..80 chars.")
    if len(description or "") > MAX_DESCRIPTION_CHARS:
        raise InvalidUsageError("Profile description exceeds 500 chars.")


class ProfileBundleStore:
    def __init__(self, home: Path) -> None:
        self._dir = Path(home) / "profile_bundles"

    def _path(self, bundle_id: str) -> Path:
        return self._dir / f"{bundle_id}.toml"

    def list(self) -> list[ProfileBundle]:
        """Every profile, the built-in default first (even with no file)."""
        bundles = []
        if self._dir.is_dir():
            for path in sorted(self._dir.glob("*.toml")):
                try:
                    bundles.append(self._read(path))
                except (OSError, tomllib.TOMLDecodeError, InvalidUsageError):
                    continue
        if not any(bundle.id == DEFAULT_PROFILE_ID for bundle in bundles):
            bundles.insert(0, default_bundle())
        else:
            bundles.sort(key=lambda bundle: bundle.id != DEFAULT_PROFILE_ID)
        return bundles

    def get(self, bundle_id: str) -> ProfileBundle:
        path = self._path(bundle_id) if PROFILE_ID_RE.match(bundle_id or "") else None
        if path is None or not path.is_file():
            if bundle_id == DEFAULT_PROFILE_ID:
                return default_bundle()
            raise NotFoundError(f"Unknown profile: {bundle_id}.")
        return self._read(path)

    def exists(self, bundle_id: str) -> bool:
        if bundle_id == DEFAULT_PROFILE_ID:
            return True
        return bool(PROFILE_ID_RE.match(bundle_id or "")) and self._path(bundle_id).is_file()

    def update(self, bundle_id: str, **changes: Any) -> ProfileBundle:
        """Change name, description, soul_id, mode or agents (None clears an option)."""
        bundle = self.get(bundle_id)
        if "name" in changes and changes["name"] is not None:
            bundle.name = str(changes["name"]).strip()
        if "description" in changes and changes["description"] is not None:
            bundle.description = str(changes["description"]).strip()
        if "soul_id" in changes:
            bundle.soul_id = str(changes["soul_id"]) if changes["soul_id"] else None
        if "mode" in changes:
            bundle.mode = str(changes["mode"]).strip().lower() if changes["mode"] else None
        if "agents" in changes and changes["agents"] is not None:
            if not isinstance(changes["agents"], dict):
                raise InvalidUsageError("Profile agents must be an object.")
            bundle.agents = {
                str(k): dict(v) for k, v in changes["agents"].items() if isinstance(v, dict)
            }
        _validate_bundle(bundle.id, bundle.name, bundle.description)
        self._write(bundle)
        return bundle

    def create(
        self,
        bundle_id: str,
        *,
        name: str,
        description: str = "",
        soul_id: str | None = None,
        mode: str | None = None,
        agents: dict[str, dict[str, Any]] | None = None,
    ) -> ProfileBundle:
        _validate_bundle(bundle_id, name, description)
        if self._path(bundle_id).exists():
            raise ConflictError(f"Profile already exists: {bundle_id}.")
        bundle = ProfileBundle(
            id=bundle_id,
            name=name.strip(),
            description=(description or "").strip(),
            soul_id=soul_id or None,
            mode=(mode or "").strip().lower() or None,
            agents={k: dict(v) for k, v in (agents or {}).items()},
        )
        self._write(bundle)
        return bundle

    def remove(self, bundle_id: str) -> None:
        if bundle_id == DEFAULT_PROFILE_ID:
            raise ConflictError("The default profile cannot be removed.")
        if not self._path(bundle_id).is_file():
            raise NotFoundError(f"Unknown profile: {bundle_id}.")
        self._path(bundle_id).unlink()

    def _read(self, path: Path) -> ProfileBundle:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise InvalidUsageError(f"Invalid profile file: {path.name}.")
        bundle_id = str(raw.get("id") or path.stem)
        name = str(raw.get("name") or "")
        description = str(raw.get("description") or "")
        _validate_bundle(bundle_id, name, description)
        soul_id = raw.get("soul_id")
        mode = raw.get("mode")
        agents = raw.get("agents") or {}
        if not isinstance(agents, dict):
            raise InvalidUsageError(f"Invalid agents map: {path.name}.")
        return ProfileBundle(
            id=bundle_id,
            name=name.strip(),
            description=description.strip(),
            soul_id=str(soul_id) if soul_id else None,
            mode=str(mode).strip().lower() if mode else None,
            agents={str(k): dict(v) for k, v in agents.items() if isinstance(v, dict)},
        )

    def _write(self, bundle: ProfileBundle) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "id": bundle.id,
            "name": bundle.name,
            "description": bundle.description,
        }
        if bundle.soul_id:
            payload["soul_id"] = bundle.soul_id
        if bundle.mode:
            payload["mode"] = bundle.mode
        if bundle.agents:
            payload["agents"] = bundle.agents
        self._path(bundle.id).write_text(tomli_w.dumps(payload), encoding="utf-8")
