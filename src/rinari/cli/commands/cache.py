"""`rinari cache` group: repository index and cache state (commands.md 53)."""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling

from ..output import emit_json, success_envelope

app = typer.Typer(help="Repository index cache.", no_args_is_help=True)


@app.command("status")
@with_error_handling("cache.status")
def status(ctx: typer.Context) -> None:
    """Show the repository index status for the current project."""
    with services(ctx) as s:
        data = s.index.status(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.status", data))
            return
        _print_status(data)


@app.command("stats")
@with_error_handling("cache.stats")
def stats(ctx: typer.Context) -> None:
    """Show index statistics (files, size, freshness)."""
    with services(ctx) as s:
        data = s.index.status(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.stats", data))
            return
        _print_status(data)


def _print_status(data) -> None:
    if not data:
        typer.echo("no index for this project")
        return
    if isinstance(data, dict):
        for key, value in data.items():
            typer.echo(f"  {key:<16} {value}")
    else:
        typer.echo(str(data))


@app.command("build")
@with_error_handling("cache.build")
def build(ctx: typer.Context) -> None:
    """Build the repository index for the current project."""
    with services(ctx) as s:
        result = s.index.build(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.build", result))
            return
        typer.echo(f"index built: {result}")


@app.command("update")
@with_error_handling("cache.update")
def update(ctx: typer.Context) -> None:
    """Incrementally update the repository index."""
    with services(ctx) as s:
        result = s.index.update(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.update", result))
            return
        typer.echo(f"index updated: {result}")


@app.command("rebuild")
@with_error_handling("cache.rebuild")
def rebuild(ctx: typer.Context) -> None:
    """Rebuild the repository index from scratch."""
    with services(ctx) as s:
        result = s.index.rebuild(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.rebuild", result))
            return
        typer.echo(f"index rebuilt: {result}")


@app.command("clear")
@with_error_handling("cache.clear")
def clear(ctx: typer.Context) -> None:
    """Clear the repository index for the current project."""
    with services(ctx) as s:
        result = s.index.clear(Path.cwd())
        if is_json(ctx):
            emit_json(success_envelope("cache.clear", {"cleared": bool(result)}))
            return
        typer.echo("index cleared")
