"""Build Manifest: versioned metadata for every product surface.

commands.md section 8 defines the `version` command fields. Surfaces that do
not exist in the current build are reported as "none (phase N)". They must
never carry a guessed version number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rinari import __version__
from rinari.application.context import AppContext
from rinari.runtime.identity import IdentityAsset, load_constitution, load_soul

HARNESS_VERSION = "1"
CONFIG_SCHEMA_VERSION = "1"
TOOL_PROTOCOL_VERSION = "1"
SESSION_EXPORT_VERSION = "1"
PLUGIN_API_VERSION = "1"
SKILL_API_VERSION = "1"


@dataclass(frozen=True, slots=True)
class BuildManifest:
    cli: str
    harness: str
    db_schema_version: int | None
    config_schema_version: str
    tool_protocol: str
    session_export: str
    plugin_api: str
    skill_api: str
    soul: IdentityAsset
    constitution: IdentityAsset

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli": self.cli,
            "harness": self.harness,
            "db_schema_version": self.db_schema_version,
            "config_schema_version": self.config_schema_version,
            "tool_protocol": self.tool_protocol,
            "session_export": self.session_export,
            "plugin_api": self.plugin_api,
            "skill_api": self.skill_api,
            "soul": _asset_dict(self.soul),
            "constitution": _asset_dict(self.constitution),
        }


def _asset_dict(asset: IdentityAsset) -> dict[str, str]:
    return {"version": asset.version, "sha256": asset.sha256, "source": asset.source}


def get_manifest(ctx: AppContext) -> BuildManifest:
    try:
        row = ctx.db.query_one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
        db_schema: int | None = row["v"] if row else 0
    except Exception:
        db_schema = None
    return BuildManifest(
        cli=__version__,
        harness=HARNESS_VERSION,
        db_schema_version=db_schema,
        config_schema_version=CONFIG_SCHEMA_VERSION,
        tool_protocol=TOOL_PROTOCOL_VERSION,
        session_export=SESSION_EXPORT_VERSION,
        plugin_api=PLUGIN_API_VERSION,
        skill_api=SKILL_API_VERSION,
        soul=load_soul(ctx.home),
        constitution=load_constitution(ctx.home),
    )
