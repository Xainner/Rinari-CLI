"""`rinari artifacts`: inspect and manage the artifact store (phase 4)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Inspect and manage artifacts (file-backed spill store).")


@app.command("list")
@with_error_handling("artifacts.list")
def artifacts_list(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Filter by session ID."),
    project: str = typer.Option(None, "--project", help="Filter by project root."),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List artifacts (newest first)."""
    with services(ctx) as s:
        items = s.artifacts.list(session_id=session, project_root=project, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("artifacts.list", [r.to_dict() for r in items]))
            return
        if not items:
            typer.echo("No artifacts.")
            return
        for record in items:
            typer.echo(
                f"{record.uri()}  {record.byte_count}B  {record.retention}  "
                f"{record.created_at}  {record.summary}"
            )


@app.command("show")
@with_error_handling("artifacts.show")
def artifacts_show(ctx: typer.Context, uri: str = typer.Argument(...)) -> None:
    """Show artifact metadata."""
    with services(ctx) as s:
        record = s.artifacts.meta(uri)
        if is_json(ctx):
            emit_json(success_envelope("artifacts.show", record.to_dict()))
            return
        for key, value in record.to_dict().items():
            typer.echo(f"{key}: {value}")


@app.command("open")
@with_error_handling("artifacts.open")
def artifacts_open(
    ctx: typer.Context,
    uri: str = typer.Argument(...),
    start: int = typer.Option(0, "--start", help="First line (0-based)."),
    end: int = typer.Option(None, "--end", help="Last line (exclusive)."),
) -> None:
    """Print artifact content (text)."""
    with services(ctx) as s:
        selected, truncated = s.artifacts.lines(uri, start, end)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "artifacts.open", {"uri": uri, "lines": selected, "truncated": truncated}
                )
            )
            return
        typer.echo("\n".join(selected))
        if truncated:
            typer.echo("... (truncated; use --start/--end or export)")


@app.command("search")
@with_error_handling("artifacts.search")
def artifacts_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """Search artifacts by name/summary/content (bounded)."""
    with services(ctx) as s:
        results = s.artifacts.search(query, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("artifacts.search", results))
            return
        if not results:
            typer.echo(f"No artifacts match: {query}")
            return
        for item in results:
            typer.echo(f"{item['uri']}  (matched {item['matched']})  {item['summary']}")


@app.command("export")
@with_error_handling("artifacts.export")
def artifacts_export(
    ctx: typer.Context,
    uri: str = typer.Argument(...),
    dest: str = typer.Option(..., "--dest", help="Destination file path."),
) -> None:
    """Copy an artifact to a local path."""
    from pathlib import Path

    with services(ctx) as s:
        target = s.artifacts.export(uri, Path(dest))
        if is_json(ctx):
            emit_json(success_envelope("artifacts.export", {"uri": uri, "path": str(target)}))
            return
        typer.echo(f"Exported to {target}")


@app.command("remove")
@with_error_handling("artifacts.remove")
def artifacts_remove(ctx: typer.Context, uri: str = typer.Argument(...)) -> None:
    """Remove an artifact (file + metadata)."""
    with services(ctx) as s:
        removed = s.artifacts.remove(uri)
        if is_json(ctx):
            emit_json(success_envelope("artifacts.remove", {"uri": uri, "removed": removed}))
            return
        typer.echo(f"Removed {uri}" if removed else f"Artifact not found: {uri}")


@app.command("gc")
@with_error_handling("artifacts.gc")
def artifacts_gc(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Only consider one session."),
) -> None:
    """Garbage-collect artifacts of gone sessions (session retention only)."""
    with services(ctx) as s:
        deleted = s.artifacts.gc(session_id=session)
        if is_json(ctx):
            emit_json(success_envelope("artifacts.gc", {"deleted": deleted}))
            return
        typer.echo(f"Deleted {deleted} orphaned artifact(s).")


__all__ = ["app"]
