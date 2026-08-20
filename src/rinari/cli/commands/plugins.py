"""`rinari plugins` group: lifecycle for contributory plugins (phase 5).

Per docs/commands.md section 45. Project-scope operations resolve the
project root from the current directory; project plugins require project
trust to load (they are recorded but inert until trusted).
"""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError, RinariError

app = typer.Typer(help="Manage plugins (tools/hook contributions).", no_args_is_help=True)

_SOURCES = ("user", "project")


def _require_source(source: str) -> None:
    if source not in _SOURCES:
        raise InvalidUsageError(f"--source must be one of: {', '.join(_SOURCES)}")


@app.command("list")
@with_error_handling("plugins.list")
def plugins_list(ctx: typer.Context) -> None:
    """List installed plugins (both scopes) with capabilities and state."""
    with services(ctx) as s:
        rows = s.plugins.list()
        if is_json(ctx):
            emit_json(success_envelope("plugins.list", {"plugins": rows}))
            return
        if not rows:
            typer.echo("No plugins installed (rinari plugins install <dir>).")
            return
        typer.echo(f"{'NAME':<20} {'SOURCE':<8} {'VERSION':<10} {'ENABLED':<8} CAPABILITIES")
        for row in rows:
            caps = ", ".join(row.get("capabilities", [])) or "-"
            typer.echo(
                f"{row['name']:<20} {row['source']:<8} {row['version']:<10} "
                f"{'yes' if row['enabled'] else 'no':<8} {caps}"
            )


@app.command("search")
@with_error_handling("plugins.search")
def plugins_search(ctx: typer.Context, query: str = typer.Argument(...)) -> None:
    """Search installed plugins by name, description or capability."""
    with services(ctx) as s:
        rows = s.plugins.list()
        terms = [t.lower() for t in query.split() if t]
        matches = [
            row
            for row in rows
            if all(
                t in row["name"].lower()
                or t
                in (row.get("manifest") or {})
                .get("description", str(row.get("capabilities")))
                .lower()
                for t in terms
            )
            if terms
        ]
        if is_json(ctx):
            emit_json(success_envelope("plugins.search", {"matches": matches}))
            return
        if not matches:
            typer.echo(f"No plugins match {query!r}.")
            return
        for row in matches:
            typer.echo(f"{row['name']}  [{row['source']}]  {row['version']}")


@app.command("show")
@with_error_handling("plugins.show")
def plugins_show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Show one plugin's record and manifest."""
    _require_source(source)
    with services(ctx) as s:
        row = s.plugins.show(name, source)
        if row is None:
            raise RinariError(f"plugin not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("plugins.show", row))
            return
        typer.echo(f"name:       {row['name']}")
        typer.echo(f"version:    {row['version']}")
        typer.echo(f"source:     {row['source']}")
        typer.echo(f"enabled:    {'yes' if row['enabled'] else 'no'}")
        typer.echo(f"path:       {row['path']}")
        manifest = row.get("manifest") or {}
        if manifest.get("description"):
            typer.echo(f"description:{manifest['description']}")
        if manifest.get("capabilities"):
            typer.echo(f"requests:   {', '.join(manifest['capabilities'])}")


@app.command("install")
@with_error_handling("plugins.install")
def plugins_install(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Directory containing plugin.json."),
    source: str = typer.Option("user", "--source", help="user | project."),
) -> None:
    """Install a plugin from a local directory (previews requested capabilities)."""
    _require_source(source)
    with services(ctx) as s:
        root = None
        if source == "project":
            root = Path.cwd()
            if s.trust.status(root).state != "trusted":
                typer.echo(
                    f"warning: {root} is not trusted; the plugin will stay inert "
                    "until `rinari trust add`"
                )
        row = s.plugins.install(Path(path), source=source, project=root)
        caps = row.get("capabilities", [])
        if is_json(ctx):
            emit_json(success_envelope("plugins.install", row))
            return
        typer.echo(f"installed {row['name']} v{row['version']} ({source})")
        typer.echo(f"requests:   {', '.join(caps) if caps else '(none)'}")


@app.command("update")
@with_error_handling("plugins.update")
def plugins_update(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    path: str = typer.Argument(..., help="Directory with the new version."),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Update an installed plugin in place from a local directory."""
    _require_source(source)
    with services(ctx) as s:
        root = Path.cwd() if source == "project" else None
        row = s.plugins.update(Path(path), name, source=source, project=root)
        if is_json(ctx):
            emit_json(success_envelope("plugins.update", row))
            return
        typer.echo(f"updated {row['name']} -> v{row['version']}")


@app.command("enable")
@with_error_handling("plugins.enable")
def plugins_enable(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Enable a plugin (its contributions load on the next session)."""
    _require_source(source)
    with services(ctx) as s:
        row = s.plugins.enable(name, source)
        if row is None:
            raise RinariError(f"plugin not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("plugins.enable", row))
            return
        typer.echo(f"enabled {name}")


@app.command("disable")
@with_error_handling("plugins.disable")
def plugins_disable(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Disable a plugin without removing it."""
    _require_source(source)
    with services(ctx) as s:
        row = s.plugins.disable(name, source)
        if row is None:
            raise RinariError(f"plugin not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("plugins.disable", row))
            return
        typer.echo(f"disabled {name}")


@app.command("remove")
@with_error_handling("plugins.remove")
def plugins_remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Remove a plugin (record + files)."""
    _require_source(source)
    with services(ctx) as s:
        root = Path.cwd() if source == "project" else None
        removed = s.plugins.remove(name, source=source, project=root)
        if is_json(ctx):
            emit_json(success_envelope("plugins.remove", {"removed": removed}))
            return
        typer.echo(f"removed {name}" if removed else f"no such plugin: {name} ({source})")


@app.command("permissions")
@with_error_handling("plugins.permissions")
def plugins_permissions(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Show the capabilities a plugin requests (trust preview)."""
    _require_source(source)
    with services(ctx) as s:
        data = s.plugins.permissions(name, source)
        if data is None:
            raise RinariError(f"plugin not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("plugins.permissions", data))
            return
        typer.echo(f"plugin {name} requests:")
        for cap in data["capabilities"]:
            typer.echo(f"  - {cap}")
        if not data["capabilities"]:
            typer.echo("  (none)")


@app.command("doctor")
@with_error_handling("plugins.doctor")
def plugins_doctor(ctx: typer.Context) -> None:
    """Diagnose load problems for every installed plugin."""
    with services(ctx) as s:
        report = s.plugins.doctor(project=Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("plugins.doctor", {"plugins": report}))
            return
        if not report:
            typer.echo("No plugins installed.")
            return
        for entry in report:
            typer.echo(f"{entry['name']}  [{entry['source']}]")
            for diag in entry["diagnostics"]:
                typer.echo(f"  {diag['code']:<20} {diag['message']}")


__all__ = ["app"]
