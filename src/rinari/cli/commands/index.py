"""`rinari index` group: repository intelligence index (phase 3)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Repository intelligence index.", no_args_is_help=True)

PathArg = typer.Argument(".", help="Project directory.")


@app.command("status")
@with_error_handling("index.status")
def index_status(ctx: typer.Context, path: str = PathArg) -> None:
    """Show index state for a project directory."""
    with services(ctx) as s:
        report = s.index.status(path)
        if is_json(ctx):
            emit_json(success_envelope("index.status", report))
            return
        if not report["indexed"]:
            typer.echo(f"{report['project_root']}: not indexed")
            typer.echo(f"  build it with: rinari index build {path}")
        else:
            meta = report["meta"]
            typer.echo(f"{report['project_root']}: indexed")
            typer.echo(
                f"  files: {meta['files']}  symbols: {meta['symbols']}"
                f"  references: {meta['references']}"
            )
            typer.echo(f"  semantic layer: {report['semantic_layer']}")
            typer.echo(f"  built: {meta['built_at']}  updated: {meta['updated_at']}")


def _print_build(command: str, report: dict, path: str) -> None:
    typer.echo(f"{path}: index {command} complete")
    typer.echo(
        f"  files: {report['total_files']}"
        f" (added {report['files_added']},"
        f" changed {report['files_changed']},"
        f" removed {report['files_removed']},"
        f" unchanged {report['files_unchanged']})"
    )
    typer.echo(f"  symbols: {report['total_symbols']}  references: {report['total_references']}")
    typer.echo(f"  duration: {report['duration_s']}s  semantic layer: {report['semantic_layer']}")


@app.command("build")
@with_error_handling("index.build")
def index_build(ctx: typer.Context, path: str = PathArg) -> None:
    """Build or incrementally update the index."""
    with services(ctx) as s:
        result = s.index.build(path)
        data = result.to_dict()
        if is_json(ctx):
            emit_json(success_envelope("index.build", data))
            return
        _print_build("build", data, path)


@app.command("update")
@with_error_handling("index.update")
def index_update(ctx: typer.Context, path: str = PathArg) -> None:
    """Incremental update (reuses the per-file hash cache)."""
    with services(ctx) as s:
        result = s.index.update(path)
        data = result.to_dict()
        if is_json(ctx):
            emit_json(success_envelope("index.update", data))
            return
        _print_build("update", data, path)


@app.command("rebuild")
@with_error_handling("index.rebuild")
def index_rebuild(ctx: typer.Context, path: str = PathArg) -> None:
    """Full rebuild (ignore the cache)."""
    with services(ctx) as s:
        result = s.index.rebuild(path)
        data = result.to_dict()
        if is_json(ctx):
            emit_json(success_envelope("index.rebuild", data))
            return
        _print_build("rebuild", data, path)


@app.command("clear")
@with_error_handling("index.clear")
def index_clear(ctx: typer.Context, path: str = PathArg) -> None:
    """Delete all index state for a project."""
    with services(ctx) as s:
        cleared = s.index.clear(path)
        if is_json(ctx):
            emit_json(success_envelope("index.clear", {"cleared": cleared}))
            return
        typer.echo(f"{path}: index cleared" if cleared else f"{path}: no index state to clear")


@app.command("search")
@with_error_handling("index.search")
def index_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Symbol name to look up."),
    path: str = PathArg,
    limit: int = typer.Option(50, "--limit", "-n", help="Max symbols/references."),
) -> None:
    """Query the index for a symbol: definitions, references, related tests."""
    with services(ctx) as s:
        result = s.index.search(path, query, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("index.search", result))
            return
        typer.echo(f"Search {query!r}:")
        if result["symbols"]:
            typer.echo("  symbols:")
            for sym in result["symbols"]:
                typer.echo(
                    f"    {sym['qualified_name']} [{sym['kind']}] {sym['rel_path']}:{sym['line']}"
                )
        else:
            typer.echo("  symbols: none")
        if result["references"]:
            typer.echo("  references:")
            for ref in result["references"]:
                typer.echo(f"    {ref['rel_path']}:{ref['line']}")
        else:
            typer.echo("  references: none")
        if result["test_files"]:
            typer.echo("  tests:")
            for test_file in result["test_files"]:
                typer.echo(f"    {test_file}")


@app.command("doctor")
@with_error_handling("index.doctor")
def index_doctor(ctx: typer.Context, path: str = PathArg) -> None:
    """Consistency report (counts + drift vs disk)."""
    with services(ctx) as s:
        report = s.index.doctor(path)
        if is_json(ctx):
            emit_json(success_envelope("index.doctor", report))
            return
        typer.echo(f"{report['project_root']}:")
        typer.echo(f"  indexed: {report['indexed']}")
        if report["meta"]:
            meta = report["meta"]
            typer.echo(
                f"  files: {meta['files']}  symbols: {meta['symbols']}"
                f"  references: {meta['references']}"
            )
            typer.echo(f"  semantic layer: {meta['semantic_layer']}")
            typer.echo(f"  built: {meta['built_at']}  updated: {meta['updated_at']}")
        typer.echo(f"  stored files: {report['stored_files']}  disk files: {report['disk_files']}")
        if report["missing_from_disk"]:
            typer.echo(f"  missing from disk: {len(report['missing_from_disk'])}")
        if report["new_on_disk"]:
            typer.echo(f"  new on disk: {len(report['new_on_disk'])}")
        if report["stale"]:
            typer.echo(f"  stale (hash drift): {len(report['stale'])}")
        typer.echo(f"  consistent: {'yes' if report['consistent'] else 'no'}")
        if not report["consistent"]:
            typer.echo("  `rinari index update` to resync.")
