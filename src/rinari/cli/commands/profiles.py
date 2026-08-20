"""`rinari profiles` group: capability/config profiles (commands.md 27)."""

from __future__ import annotations

import re

import typer

from rinari.application.config import profiles as profile_mod
from rinari.application.config import writer
from rinari.cli.deps import app_context, is_json, with_error_handling
from rinari.shared.errors import ConfigurationError, InvalidUsageError, NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Capability and config profiles.", no_args_is_help=True)

_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]*$")


def _profiles_dir(c) -> object:
    return c.layout.dir("profiles")


def _is_builtin(name: str) -> bool:
    return name in profile_mod.BUILTIN_PROFILES


def _read_user_profile(c, name: str) -> dict:
    path = _profiles_dir(c) / f"{name}.toml"
    if not path.is_file():
        raise NotFoundError(f"Profile not found: {name}")
    return profile_mod.load_profile(name, _profiles_dir(c))


@app.command("list")
@with_error_handling("profiles.list")
def profiles_list(ctx: typer.Context) -> None:
    """List built-in profiles and user profile files."""
    with app_context(ctx) as c:
        names = profile_mod.available_profiles(_profiles_dir(c))
        current = c.config.active_profile_name()
        rows = [
            {
                "name": n,
                "source": "builtin" if _is_builtin(n) else "user",
                "active": n == current,
            }
            for n in names
        ]
        if is_json(ctx):
            emit_json(success_envelope("profiles.list", rows))
            return
        for row in rows:
            marker = "*" if row["active"] else " "
            typer.echo(f" {marker} {row['name']:<16} ({row['source']})")


@app.command("show")
@with_error_handling("profiles.show")
def profiles_show(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Profile name (default: active)."),
) -> None:
    """Show a profile's key overrides."""
    with app_context(ctx) as c:
        target = name or c.config.active_profile_name()
        data = profile_mod.load_profile(target, _profiles_dir(c))
        source = "builtin" if _is_builtin(target) else "user"
        if is_json(ctx):
            emit_json(
                success_envelope("profiles.show", {"name": target, "source": source, "data": data})
            )
            return
        typer.echo(f"{target}  [{source}]")
        if not data:
            typer.echo("  (inherits everything from lower config layers)")
        for key, value in sorted(data.items()):
            typer.echo(f"  {key} = {value}")


@app.command("create")
@with_error_handling("profiles.create")
def profiles_create(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Create a new user profile file with a minimal template."""
    if not _NAME_RE.match(name):
        raise InvalidUsageError("Profile names must match ^[a-z][a-z0-9-]*$")
    with app_context(ctx) as c:
        path = _profiles_dir(c) / f"{name}.toml"
        if path.exists():
            raise ConfigurationError(f"Profile already exists: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Rinari profile: {name}\n# Keys here override user config (commands.md 26).\n"
            'permissions = { profile = "workspace" }\n',
            encoding="utf-8",
        )
        if is_json(ctx):
            emit_json(success_envelope("profiles.create", {"name": name, "path": str(path)}))
        else:
            typer.echo(f"created {path}")


@app.command("clone")
@with_error_handling("profiles.clone")
def profiles_clone(
    ctx: typer.Context,
    src: str = typer.Argument(...),
    dst: str = typer.Argument(...),
) -> None:
    """Clone a profile into a new user profile file."""
    import tomli_w

    if not _NAME_RE.match(dst):
        raise InvalidUsageError("Profile names must match ^[a-z][a-z0-9-]*$")
    with app_context(ctx) as c:
        data = profile_mod.load_profile(src, _profiles_dir(c))
        path = _profiles_dir(c) / f"{dst}.toml"
        if path.exists():
            raise ConfigurationError(f"Profile already exists: {dst}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomli_w.dumps(data or {}), encoding="utf-8")
        if is_json(ctx):
            emit_json(
                success_envelope("profiles.clone", {"source": src, "name": dst, "path": str(path)})
            )
        else:
            typer.echo(f"cloned {src} -> {path}")


@app.command("edit")
@with_error_handling("profiles.edit")
def profiles_edit(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    set_value: list[str] = typer.Option(
        None, "--set", "-s", help="key=value (repeatable; dotted keys allowed)."
    ),
    unset: list[str] = typer.Option(None, "--unset", help="Dotted key to remove (repeatable)."),
) -> None:
    """Edit a user profile's keys non-interactively."""
    import tomli_w

    if _is_builtin(name):
        raise InvalidUsageError(
            f"Built-in profile: {name}", hint="Clone it first (rinari profiles clone)."
        )
    if not set_value and not unset:
        raise InvalidUsageError("Nothing to edit", hint="Use --set key=value and/or --unset key.")
    with app_context(ctx) as c:
        data = dict(_read_user_profile(c, name))
        for item in set_value or []:
            if "=" not in item:
                raise InvalidUsageError(f"--set expects key=value, got: {item}")
            key, value = item.split("=", 1)
            writer.set_dotted(data, key.strip(), writer.parse_value(value.strip(), key.strip()))
        for key in unset or []:
            updated, existed = writer.unset_dotted(data, key.strip())
            if not existed:
                raise NotFoundError(f"{key} is not set in profile {name}")
            data = updated
        path = _profiles_dir(c) / f"{name}.toml"
        path.write_text(tomli_w.dumps(data), encoding="utf-8")
        if is_json(ctx):
            emit_json(success_envelope("profiles.edit", {"name": name, "path": str(path)}))
        else:
            typer.echo(f"updated {path}")


@app.command("use")
@with_error_handling("profiles.use")
def profiles_use(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Select the profile used by new sessions."""
    with app_context(ctx) as c:
        profile_mod.load_profile(name, _profiles_dir(c))  # validates existence
        user = writer.read_user_data(c.layout)
        updated = writer.set_dotted(user, "profile", name)
        write_path = writer.write_user_data(c.layout, updated)
        if is_json(ctx):
            emit_json(success_envelope("profiles.use", {"profile": name, "path": str(write_path)}))
        else:
            typer.echo(f"profile = {name} ({write_path})")


@app.command("remove")
@with_error_handling("profiles.remove")
def profiles_remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Remove a user profile file (built-ins cannot be removed)."""
    if _is_builtin(name):
        raise InvalidUsageError(f"Built-in profile: {name}")
    with app_context(ctx) as c:
        path = _profiles_dir(c) / f"{name}.toml"
        if not path.is_file():
            raise NotFoundError(f"Profile not found: {name}")
        if c.config.active_profile_name() == name:
            raise InvalidUsageError(
                "Profile is active", hint="Select another profile first (rinari profiles use)."
            )
        path.unlink()
        if is_json(ctx):
            emit_json(success_envelope("profiles.remove", {"name": name}))
        else:
            typer.echo(f"removed {name}")
