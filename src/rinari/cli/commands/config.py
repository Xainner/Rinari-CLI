import json

import typer

from rinari.application.config import writer
from rinari.application.config.schema import key_type, leaf_keys
from rinari.cli.deps import app_context, with_error_handling
from rinari.shared.errors import InvalidUsageError, NotFoundError

app = typer.Typer(help="Manage Rinari configuration.", no_args_is_help=True)


def _fail_unless_known_key(key: str) -> None:
    if key not in leaf_keys():
        raise InvalidUsageError(f"Unknown config key: {key}", hint="See `rinari config list`.")


@app.command("path")
@with_error_handling
def config_path(ctx: typer.Context) -> None:
    """Print the user config file path."""
    with app_context(ctx) as c:
        typer.echo(str(c.layout.config_file))


@app.command("list")
@with_error_handling
def config_list(
    ctx: typer.Context,
    show_layers: bool = typer.Option(
        False, "--layers", help="Show the contributing layer per key."
    ),
) -> None:
    """List effective configuration values."""
    with app_context(ctx) as c:
        for key in leaf_keys():
            value = c.config.value(key)
            line = f"{key} = {_fmt(value)}"
            if show_layers:
                sources = ", ".join(name for name, _ in c.config.sources_for(key)) or "-"
                line += f"   [{sources}]"
            typer.echo(line)


@app.command("get")
@with_error_handling
def config_get(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    explain: bool = typer.Option(False, "--explain", help="Show every layer that defines the key."),
) -> None:
    """Read a config value by dotted key (e.g. agent.max_turns)."""
    _fail_unless_known_key(key)
    with app_context(ctx) as c:
        if explain:
            for name, value in c.config.sources_for(key) or [("-", None)]:
                typer.echo(f"{name}: {_fmt(value)}")
            return
        typer.echo(_fmt(c.config.value(key)))


@app.command("set")
@with_error_handling
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
        typer.echo(f"Set {key} = {_fmt(parsed)} ({write_path})")


@app.command("unset")
@with_error_handling
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
        typer.echo(f"Unset {key} ({write_path})")


@app.command("validate")
@with_error_handling
def config_validate(ctx: typer.Context) -> None:
    """Validate the effective configuration (defaults + user + profile + project)."""
    with app_context(ctx) as c:  # loading the context already validates the merge
        sources = ", ".join(layer.name for layer in c.config.layers)
        typer.echo(f"Config OK ({len(leaf_keys())} keys; layers: {sources})")


def _fmt(value: object) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True)
