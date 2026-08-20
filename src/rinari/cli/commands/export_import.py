"""Top-level `rinari export` / `rinari import` (commands.md 55/56).

Export: session documents, effective config (secrets redacted), skills,
profiles. Import: session documents and config bundles. Secret values are
never exported, only references; redacted leaves are rejected on import.
"""

from __future__ import annotations

import json
from pathlib import Path

import tomli_w
import typer

from rinari.application.config import profiles as profile_mod
from rinari.application.config import writer
from rinari.cli.deps import app_context, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import session_dict
from rinari.cli.session_export import export_session, import_session
from rinari.shared.errors import ConfigurationError, InvalidUsageError

export_app = typer.Typer(help="Export state as portable documents.", no_args_is_help=True)
import_app = typer.Typer(help="Import documents exported by Rinari.", no_args_is_help=True)

_SECRET_KEY_PARTS = ("key", "secret", "token", "password", "credential")
CONFIG_EXPORT_VERSION = "1"


def _json(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str)


def _emit_json_or_file(ctx: typer.Context, command: str, document: dict, out: str | None) -> None:
    text = _json(document)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        if is_json(ctx):
            emit_json(success_envelope(command, {"path": out}))
        else:
            typer.echo(f"exported -> {out}")
        return
    if is_json(ctx):
        typer.echo(_json({**document}))
    else:
        typer.echo(text)


def _redact(node, key_path: str):
    if isinstance(node, dict):
        return {k: _redact(v, f"{key_path}.{k}".lstrip(".")) for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(v, key_path) for v in node]
    if (
        isinstance(node, str)
        and key_path
        and any(part in key_path.lower() for part in _SECRET_KEY_PARTS)
    ):
        if node.startswith("${") and node.endswith("}"):
            return node  # environment-variable reference, safe
        return "[redacted]"
    return node


# --- export ---------------------------------------------------------------


@export_app.command("session")
@with_error_handling("export.session")
def export_session_cmd(
    ctx: typer.Context,
    ref: str = typer.Argument(None, help="Session ID / prefix (default: most recent)."),
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export a session document (record, messages, events)."""
    with services(ctx) as s:
        document = export_session(s, ref)
    _emit_json_or_file(ctx, "export.session", document, out)


@export_app.command("config")
@with_error_handling("export.config")
def export_config(
    ctx: typer.Context,
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export the effective config with secrets redacted."""
    with app_context(ctx) as c:
        data = c.config.raw()
    document = {
        "version": CONFIG_EXPORT_VERSION,
        "source": "rinari-export-config",
        "profile": c.config.active_profile_name(),
        "data": _redact(data, ""),
    }
    _emit_json_or_file(ctx, "export.config", document, out)


@export_app.command("skills")
@with_error_handling("export.skills")
def export_skills(
    ctx: typer.Context,
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export the installed skill manifests (no content)."""
    with services(ctx) as s:
        rows = [
            {
                "name": row.get("name"),
                "version": row.get("version"),
                "source": row.get("source"),
                "active": row.get("active", False),
            }
            for row in s.skills.summaries()
        ]
    document = {"version": CONFIG_EXPORT_VERSION, "source": "rinari-export-skills", "skills": rows}
    _emit_json_or_file(ctx, "export.skills", document, out)


@export_app.command("profile")
@with_error_handling("export.profile")
def export_profile(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Profile name (default: active)."),
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export one profile's overrides."""
    with app_context(ctx) as c:
        target = name or c.config.active_profile_name()
        profiles_dir = c.layout.dir("profiles")
        data = profile_mod.load_profile(target, profiles_dir)
    document = {
        "version": CONFIG_EXPORT_VERSION,
        "source": "rinari-export-profile",
        "name": target,
        "data": data,
    }
    _emit_json_or_file(ctx, "export.profile", document, out)


# --- import ---------------------------------------------------------------


@import_app.command("session")
@with_error_handling("import.session")
def import_session_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Session document file."),
) -> None:
    """Import a session document (new session id is assigned)."""
    if not Path(path).is_file():
        raise InvalidUsageError(f"File not found: {path}")
    with services(ctx) as s:
        record = import_session(s, Path(path).read_text(encoding="utf-8"))
        if is_json(ctx):
            emit_json(success_envelope("import.session", session_dict(record)))
            return
        typer.echo(f"imported {record.id} ({record.kind})")


@import_app.command("config")
@with_error_handling("import.config")
def import_config(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Config bundle file (export config)."),
    out: str = typer.Option(None, "--out", help="Preview the merge without writing."),
) -> None:
    """Import a config bundle: applies non-redacted leaves to user config."""
    if not Path(path).is_file():
        raise InvalidUsageError(f"File not found: {path}")
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidUsageError(f"Not a valid JSON document: {exc}") from exc
    if document.get("source") != "rinari-export-config":
        raise ConfigurationError("Not a Rinari config bundle", hint="Use `rinari export config`.")
    data = document.get("data") or {}

    dotted_keys: list[str] = []

    def _walk(node, key_path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{key_path}.{k}".lstrip("."))
        else:
            if node == "[redacted]":
                raise InvalidUsageError(
                    f"Refusing to import redacted value for {key_path!r}",
                    hint="Set the real value manually (rinari config set / profiles edit).",
                )
            if isinstance(node, str) and node.startswith("${") and node.endswith("}"):
                return  # env references point at the importing host; skip
            dotted_keys.append((key_path, node))

    _walk(data, "")
    count = len(dotted_keys)

    with app_context(ctx) as c:
        user = writer.read_user_data(c.layout)
        for dotted, value in dotted_keys:
            user = writer.set_dotted(user, dotted, value)
        text = tomli_w.dumps(user)

    if out:
        Path(out).write_text(text, encoding="utf-8")
        if is_json(ctx):
            emit_json(success_envelope("import.config", {"path": out, "keys": count}))
        else:
            typer.echo(f"config preview -> {out} ({count} keys)")
        return
    writer.write_user_data(c.layout, user)
    if is_json(ctx):
        emit_json(
            success_envelope(
                "import.config", {"keys": count, "user_config": str(c.layout.config_file)}
            )
        )
    else:
        typer.echo(f"imported {count} config keys -> {c.layout.config_file}")
