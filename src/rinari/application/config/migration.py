"""Deterministic migration of the v1-legacy config layout to the modern schema.

The v1 layout stored inline `[default]` and `[profile.<name>]` endpoint tables
(base_url/model/provider/temperature/api_key) plus a `[user]` table in
config.toml. The modern layout keeps only schema-known keys in config.toml;
endpoints are provider/model records with secrets in the credential store.

The migration is explicit, non-destructive and secret-safe:

- an exact text backup (``config.toml.bak-<stamp>``) is written first;
- only schema-known values are carried into the new config.toml;
- endpoint tables are removed from the live config but reported back with the
  exact recreate commands;
- API key values are never echoed, duplicated or written anywhere new — the
  only surviving copy stays in the untouched backup.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.application.config.schema import Config
from rinari.application.config.writer import _with_defaults, write_user_data
from rinari.shared.errors import ConfigurationError
from rinari.shared.paths import HomeLayout

MODERN_SCALARS = ("user_name", "language", "model", "profile")
MODERN_TABLES = (
    "agent",
    "context",
    "agents",
    "permissions",
    "network",
    "workspace",
    "instructions",
    "telemetry",
)

_SECRET_KEY_PARTS = ("key", "secret", "token", "password", "credential")
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class LegacyEndpoint:
    name: str
    base_url: str | None
    model: str | None
    provider: str | None
    temperature: Any
    secret_env: str | None
    has_secret: bool

    @property
    def model_alias(self) -> str:
        slug = re.sub(r"[^a-z0-9_-]+", "-", (self.model or "model").lower()).strip("-")
        if not slug:
            return f"{self.name}-model"
        return f"{self.name}-{slug}"

    def recreate_commands(self) -> list[str]:
        commands: list[str] = []
        parts = [f"rinari providers add custom --name {self.name}"]
        if self.base_url:
            parts.append(f"--endpoint {self.base_url}")
        if self.secret_env:
            parts.append(f"--api-key-env {self.secret_env}")
        elif self.has_secret:
            parts.append("--api-key <re-enter from backup>")
        else:
            parts.append("--no-auth")
        commands.append(" ".join(parts))
        if self.model:
            commands.append(
                f"rinari models add --provider {self.name} --model {self.model} "
                f"--name {self.model_alias}"
            )
            commands.append(f"rinari provider use {self.name}")
            commands.append(f"rinari model use {self.model_alias}")
        return commands


@dataclass(frozen=True)
class LegacyMigrationReport:
    migrated: bool
    reason: str
    backup: Path | None
    kept: dict[str, Any] = field(default_factory=dict)
    endpoints: list[LegacyEndpoint] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "migrated": self.migrated,
            "reason": self.reason,
            "backup": str(self.backup) if self.backup else None,
            "kept": self.kept,
            "endpoints": [
                {
                    "name": e.name,
                    "base_url": e.base_url,
                    "model": e.model,
                    "provider": e.provider,
                    "temperature": e.temperature,
                    "secret_env": e.secret_env,
                    "has_secret": e.has_secret,
                    "recreate_commands": e.recreate_commands(),
                }
                for e in self.endpoints
            ],
            "next_steps": self.next_steps,
            "warnings": self.warnings,
        }


def _endpoint(name: str, table: dict[str, Any]) -> LegacyEndpoint:
    base_url = table.get("base_url")
    model = table.get("model")
    provider = table.get("provider")
    temperature = table.get("temperature")
    secret_env: str | None = None
    has_secret = False
    for key, value in table.items():
        if any(part in key.lower() for part in _SECRET_KEY_PARTS):
            has_secret = True
            if isinstance(value, str):
                match = _ENV_REF.match(value)
                if match:
                    secret_env = match.group(1)
    return LegacyEndpoint(
        name=name,
        base_url=base_url if isinstance(base_url, str) else None,
        model=model if isinstance(model, str) else None,
        provider=provider if isinstance(provider, str) else None,
        temperature=temperature,
        secret_env=secret_env,
        has_secret=has_secret,
    )


def _profile_is_modern(name: str, profiles_dir: Path) -> bool:
    from rinari.application.config.profiles import BUILTIN_PROFILES

    return name in BUILTIN_PROFILES or (profiles_dir / f"{name}.toml").is_file()


def plan_migration(raw: dict[str, Any], profiles_dir: Path) -> LegacyMigrationReport:
    """Pure planning step: what survives, what needs recreating."""
    kept: dict[str, Any] = {}
    for key in MODERN_SCALARS:
        value = raw.get(key)
        if isinstance(value, (str, int, float, bool)) and value:
            kept[key] = value
    for section in MODERN_TABLES:
        value = raw.get(section)
        if isinstance(value, dict) and value:
            kept[section] = dict(value)

    warnings: list[str] = []
    profile_value = raw.get("profile")
    if isinstance(profile_value, dict):
        profile_value = None
    if (
        isinstance(profile_value, str)
        and profile_value
        and not _profile_is_modern(profile_value, profiles_dir)
    ):
        warnings.append(
            f"profile reference {profile_value!r} has no matching profile file; "
            "dropped (defaults to builtin 'workspace')."
        )
        kept.pop("profile", None)

    endpoints: list[LegacyEndpoint] = []
    default = raw.get("default")
    if isinstance(default, dict) and default:
        endpoints.append(_endpoint("default", default))
    inline_profiles = raw.get("profile")
    if isinstance(inline_profiles, dict):
        for name, table in inline_profiles.items():
            if isinstance(table, dict) and table:
                endpoints.append(_endpoint(str(name), table))

    user_name: str | None = None
    user = raw.get("user")
    if isinstance(user, dict) and isinstance(user.get("name"), str) and user["name"]:
        user_name = user["name"]
        if not kept.get("user_name"):
            kept["user_name"] = user_name

    has_legacy = bool(endpoints) or "user" in raw or isinstance(raw.get("profile"), dict)
    if not has_legacy:
        return LegacyMigrationReport(
            migrated=False,
            reason="no legacy config detected",
            backup=None,
            kept=kept,
        )

    next_steps: list[str] = []
    for endpoint in endpoints:
        for command in endpoint.recreate_commands():
            next_steps.append(command)
    next_steps.append(
        "After recreating providers: `rinari config set profile workspace` "
        "(or create a profile file) and review the backup before deleting it."
    )
    return LegacyMigrationReport(
        migrated=True,
        reason="legacy v1 layout",
        backup=None,
        kept=kept,
        endpoints=endpoints,
        next_steps=next_steps,
        warnings=warnings,
    )


def migrate_legacy_config(
    layout: HomeLayout, stamp: str, *, dry_run: bool = False
) -> LegacyMigrationReport:
    path = layout.config_file
    if not path.is_file():
        return LegacyMigrationReport(migrated=False, reason="no user config file", backup=None)
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"User config {path} is not valid TOML; cannot plan a migration: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        return LegacyMigrationReport(
            migrated=False, reason="no legacy config detected", backup=None
        )

    report = plan_migration(raw, layout.dir("profiles"))
    if not report.migrated:
        return report
    if dry_run:
        return report

    # Validate the deep-merged config (defaults under kept) before writing.
    Config.from_dict(_with_defaults(report.kept))
    backup = path.with_name(f"config.toml.bak-{stamp}")
    backup.write_text(text, encoding="utf-8")
    write_user_data(layout, report.kept)
    return LegacyMigrationReport(
        migrated=True,
        reason=report.reason,
        backup=backup,
        kept=report.kept,
        endpoints=report.endpoints,
        next_steps=report.next_steps,
        warnings=report.warnings,
    )
