import json

import typer

from rinari.application.config import writer
from rinari.application.config.schema import key_type, leaf_keys
from rinari.cli.deps import app_context, home_layout, is_json, with_error_handling
from rinari.shared.errors import InvalidUsageError, NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Manage Rinari configuration.", no_args_is_help=True)


def _fail_unless_known_key(key: str) -> None:
    if key not in leaf_keys():
        raise InvalidUsageError(f"Unknown config key: {key}", hint="See `rinari config list`.")


@app.command("path")
@with_error_handling("config.path")
def config_path(ctx: typer.Context) -> None:
    """Print the user config file path."""
    with app_context(ctx) as c:
        path = str(c.layout.config_file)
        if is_json(ctx):
            emit_json(success_envelope("config.path", {"path": path}))
        else:
            typer.echo(path)


@app.command("list")
@with_error_handling("config.list")
def config_list(
    ctx: typer.Context,
    show_layers: bool = typer.Option(
        False, "--layers", help="Show the contributing layer per key."
    ),
) -> None:
    """List effective configuration values."""
    with app_context(ctx) as c:
        entries = []
        for key in leaf_keys():
            entry = {"key": key, "value": _jsonable(c.config.value(key))}
            if show_layers:
                entry["sources"] = [name for name, _ in c.config.sources_for(key)]
            entries.append(entry)
        if is_json(ctx):
            emit_json(success_envelope("config.list", entries))
            return
        for key in leaf_keys():
            value = c.config.value(key)
            line = f"{key} = {_fmt(value)}"
            if show_layers:
                sources = ", ".join(name for name, _ in c.config.sources_for(key)) or "-"
                line += f"   [{sources}]"
            typer.echo(line)


@app.command("get")
@with_error_handling("config.get")
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    explain: bool = typer.Option(False, "--explain", help="Show every layer that defines the key."),
) -> None:
    """Read a config value by dotted key (e.g. agent.max_turns)."""
    _fail_unless_known_key(key)
    with app_context(ctx) as c:
        if explain:
            layers = [
                {"source": name, "value": _jsonable(value)}
                for name, value in c.config.sources_for(key) or [("-", None)]
            ]
            if is_json(ctx):
                emit_json(success_envelope("config.get", {"key": key, "sources": layers}))
                return
            for name, value in c.config.sources_for(key) or [("-", None)]:
                typer.echo(f"{name}: {_fmt(value)}")
            return
        value = c.config.value(key)
        if is_json(ctx):
            emit_json(success_envelope("config.get", {"key": key, "value": _jsonable(value)}))
        else:
            typer.echo(_fmt(value))


@app.command("set")
@with_error_handling("config.set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    value: str = typer.Argument(...),
) -> None:
    """Set a value in the user config. The full schema is validated before persistence."""
    _fail_unless_known_key(key)
    declared = key_type(key)
    parsed = writer.parse_value(value, key)
    if declared == "list[str]" and not parsed:
        raise InvalidUsageError(f"list key {key} must not be empty")
    with app_context(ctx) as c:
        user = writer.read_user_data(c.layout)
        updated = writer.set_dotted(user, key, parsed)
        write_path = writer.write_user_data(c.layout, updated)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "config.set", {"key": key, "value": _jsonable(parsed), "path": str(write_path)}
                )
            )
        else:
            typer.echo(f"Set {key} = {_fmt(parsed)} ({write_path})")


@app.command("unset")
@with_error_handling("config.unset")
def config_unset(
    ctx: typer.Context,
    key: str = typer.Argument(...),
) -> None:
    """Remove a user-config override so the value falls back to its default."""
    _fail_unless_known_key(key)
    with app_context(ctx) as c:
        user = writer.read_user_data(c.layout)
        updated, existed = writer.unset_dotted(user, key)
        if not existed:
            raise NotFoundError(f"{key} is not set in the user config")
        write_path = writer.write_user_data(c.layout, updated)
        if is_json(ctx):
            emit_json(success_envelope("config.unset", {"key": key, "path": str(write_path)}))
        else:
            typer.echo(f"Unset {key} ({write_path})")


@app.command("validate")
@with_error_handling("config.validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the effective configuration (defaults + user + profile + project)."""
    with app_context(ctx) as c:  # loading the context already validates the merge
        layers = [layer.name for layer in c.config.layers]
        if is_json(ctx):
            emit_json(
                success_envelope("config.validate", {"keys": len(leaf_keys()), "layers": layers})
            )
        else:
            typer.echo(f"Config OK ({len(leaf_keys())} keys; layers: {', '.join(layers)})")


@app.command("migrate")
@with_error_handling("config.migrate")
def config_migrate(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan without writing."),
) -> None:
    """Migrate the v1-legacy config layout (inline [default]/[profile.*] endpoint
    tables, [user] table) to the modern schema. Backs up the original, keeps
    schema-known values, and reports exact recreate commands for endpoints.
    API keys are never echoed or duplicated; the backup is the only surviving
    copy."""
    import time

    from rinari.application.config.migration import migrate_legacy_config

    with home_layout(ctx) as layout:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        report = migrate_legacy_config(layout, stamp, dry_run=dry_run)
        if is_json(ctx):
            emit_json(success_envelope("config.migrate", report.to_dict()))
            return
        if not report.migrated:
            typer.echo(f"Nothing to migrate: {report.reason}")
            return
        if dry_run:
            typer.echo("[dry-run] would migrate the v1-legacy config layout:")
            typer.echo(f"  kept keys: {sorted(report.kept) or '(none)'}")
        else:
            typer.echo(f"Migrated legacy config -> {layout.config_file}")
            typer.echo(f"Backup: {report.backup}")
        for endpoint in report.endpoints:
            typer.echo(f"Endpoint {endpoint.name} (recreate, key comes from the backup):")
            for command in endpoint.recreate_commands():
                typer.echo(f"  {command}")
        for warning in report.warnings:
            typer.echo(f"warning: {warning}")


def _jsonable(value: object) -> object:
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _fmt(value: object) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True)
