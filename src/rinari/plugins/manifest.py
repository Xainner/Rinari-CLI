"""Plugin manifest: schema, loader and validation (phase 5)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_SOURCES = ("user", "project")
CONTRIBUTION_TYPES = (
    "tools",
    "skills",
    "provider.adapters",
    "commands",
    "hooks",
    "agents",
    "context.provide",
)
# Contribution types actually registered by the runtime (first pass).
SUPPORTED_CONTRIBUTIONS = ("tools", "hooks")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")


class PluginError(Exception):
    """Structured plugin failure (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # MANIFEST_INVALID | NAME_INVALID | VERSION_INVALID | ENTRYPOINT_MISSING
        # | UNSUPPORTED_CONTRIBUTION | TRUST_REQUIRED | LOAD_FAILED | NOT_FOUND
        # | ALREADY_EXISTS | REMOVAL_FAILED
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    description: str = ""
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def requests(self, contribution: str) -> bool:
        return contribution in self.capabilities


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = Path(plugin_dir) / "plugin.json"
    if not path.is_file():
        raise PluginError("MANIFEST_INVALID", f"missing plugin manifest: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError("MANIFEST_INVALID", f"unreadable plugin.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginError("MANIFEST_INVALID", "plugin.json must be a JSON object")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PluginError("NAME_INVALID", f"invalid plugin name: {name!r} (use [a-z0-9_-], max 64)")
    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PluginError("VERSION_INVALID", f"invalid plugin version: {version!r}")
    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.endswith(".py"):
        raise PluginError(
            "MANIFEST_INVALID",
            f"entrypoint must be a .py file name inside the plugin: {entrypoint!r}",
        )
    capabilities = raw.get("capabilities", [])
    if capabilities is None:
        capabilities = []
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise PluginError("MANIFEST_INVALID", "capabilities must be a list of strings")
    unknown = [c for c in capabilities if c not in CONTRIBUTION_TYPES]
    if unknown:
        raise PluginError(
            "MANIFEST_INVALID",
            f"unknown capability type(s): {', '.join(unknown)} "
            f"(valid: {', '.join(CONTRIBUTION_TYPES)})",
        )
    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise PluginError("MANIFEST_INVALID", "description must be a string")
    return PluginManifest(
        name=name,
        version=version,
        entrypoint=entrypoint,
        description=description,
        capabilities=tuple(sorted(set(capabilities))),
    )


__all__ = [
    "CONTRIBUTION_TYPES",
    "SUPPORTED_CONTRIBUTIONS",
    "VALID_SOURCES",
    "PluginError",
    "PluginManifest",
    "load_manifest",
]
