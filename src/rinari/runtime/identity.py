"""Runtime identity assets: Soul and Constitution resolution.

Resolution follows harness.md section 2:

    Soul:         <home>/soul.md          (user override)
    fallback:     packaged assets/soul.md

    Constitution: <home>/constitution.md  (user override)
    fallback:     packaged assets/constitution.md

Packaged canonical assets are never mutated; a user override only replaces
the document loaded for this installation. Each asset carries version and
sha256 metadata so prompt-stack composition can be traced and diffed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from rinari.shared.errors import ConfigurationError
from rinari.soul.store import SoulDefinition

_ASSET_HEADER = re.compile(
    r"<!--\s*rinari-asset\s*:\s*id=(?P<id>[a-z0-9_-]+)\s+version=(?P<version>[\w.\-]+)\s*-->"
)

SOURCE_PACKAGED = "packaged"
SOURCE_USER = "user"


@dataclass(frozen=True, slots=True)
class IdentityAsset:
    name: str
    version: str
    sha256: str
    source: str
    path: Path
    text: str


def load_soul(home: str | Path) -> IdentityAsset:
    return _load("soul", Path(home))


def load_active_soul(home: str | Path) -> IdentityAsset:
    """Soul 3.0 resolution: active custom soul → legacy ~/soul.md → bundled default.

    An explicit legacy `soul.md` override keeps working untouched (migration
    compatibility): it wins over the bundled default but loses to an explicit
    activation. Nothing is moved or rewritten silently.
    """
    from rinari.soul.store import DEFAULT_SOUL_ID, SoulStore

    home_path = Path(home)
    store = SoulStore(home_path)
    active = store.active_id()
    if active is not None:
        try:
            definition = store.get(active)
        except Exception:
            definition = None
        if definition is not None:
            return _from_definition(definition, home_path)
    legacy = home_path / "soul.md"
    if legacy.is_file():
        asset = _load("soul", home_path)
        return asset
    try:
        definition = store.get(DEFAULT_SOUL_ID)
    except Exception:
        return _load("soul", home_path)
    return _from_definition(definition, home_path)


def _from_definition(definition: SoulDefinition, home: Path) -> IdentityAsset:
    if definition.source == "custom":
        source = SOURCE_USER
        path = home / "souls" / definition.id / "identity.md"
    else:
        source = SOURCE_PACKAGED
        path = Path(str(files("rinari").joinpath(f"assets/souls/{definition.id}/identity.md")))
    return IdentityAsset(
        name=f"soul:{definition.id}",
        version=definition.version,
        sha256=hashlib.sha256(definition.identity.encode("utf-8")).hexdigest(),
        source=source,
        path=path,
        text=definition.identity,
    )


def load_constitution(home: str | Path) -> IdentityAsset:
    return _load("constitution", Path(home))


def _load(name: str, home: Path) -> IdentityAsset:
    override = home / f"{name}.md"
    if override.is_file():
        path = override
        source = SOURCE_USER
    else:
        path = Path(str(files("rinari").joinpath(f"assets/{name}.md")))
        source = SOURCE_PACKAGED
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read {name} asset at {path}") from exc
    if not text.strip():
        raise ConfigurationError(f"{name} asset at {path} is empty")
    match = _ASSET_HEADER.search(text[:512])
    version = match.group("version") if match else "unknown"
    return IdentityAsset(
        name=name,
        version=version,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source=source,
        path=path,
        text=text,
    )
