"""`rinari context`: inspect and manage context retrieval and pins (phase 4)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Inspect and manage context retrieval and pins.")

_PIN_SOURCES = ("file", "symbol", "memory", "artifact", "term")


@app.command("pins")
@with_error_handling("context.pins")
def context_pins(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """List pinned context items for a session."""
    with services(ctx) as s:
        pins = s.retrieval.list_pins(session)
        if is_json(ctx):
            emit_json(success_envelope("context.pins", {"session": session, "pins": pins}))
            return
        if not pins:
            typer.echo("No pinned context for this session.")
            return
        for pin in pins:
            label = f"  ({pin['label']})" if pin["label"] else ""
            typer.echo(f"  [{pin['source']}] {pin['pin_ref']}{label}  {pin['created_at']}")


@app.command("pin")
@with_error_handling("context.pin")
def context_pin(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ..., help="Path (file), symbol name, memory id, artifact URI, or term."
    ),
    source: str = typer.Option(..., "--source"),
    label: str = typer.Option("", "--label"),
    session: str = typer.Option(..., "--session"),
) -> None:
    """Pin a context item so it stays model-visible every session turn."""
    if source not in _PIN_SOURCES:
        typer.echo(f"invalid source: {source} (use one of {', '.join(_PIN_SOURCES)})", err=True)
        raise typer.Exit(2)
    with services(ctx) as s:
        row = s.retrieval.pin(session, source, ref, label)
        if is_json(ctx):
            emit_json(success_envelope("context.pin", row))
            return
        typer.echo(f"pinned [{row['source']}] {row['pin_ref']} in session {session}")


@app.command("unpin")
@with_error_handling("context.unpin")
def context_unpin(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    source: str = typer.Option(..., "--source"),
    session: str = typer.Option(..., "--session"),
) -> None:
    """Remove a pinned context item."""
    if source not in _PIN_SOURCES:
        typer.echo(f"invalid source: {source} (use one of {', '.join(_PIN_SOURCES)})", err=True)
        raise typer.Exit(2)
    with services(ctx) as s:
        unpinned = s.retrieval.unpin(session, source, ref)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "context.unpin", {"source": source, "ref": ref, "unpinned": unpinned}
                )
            )
            return
        typer.echo(f"unpinned [{source}] {ref}" if unpinned else f"pin not found: [{source}] {ref}")


@app.command("retrieve")
@with_error_handling("context.retrieve")
def context_retrieve(
    ctx: typer.Context,
    query: str = typer.Argument(
        "", help="Query to rank candidates against (empty = list top items)."
    ),
    session: str = typer.Option(None, "--session", help="Session for pins/artifacts context."),
    project: str = typer.Option(None, "--project", help="Project root for index/memory sources."),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    """Retrieve ranked, deduplicated context candidates for a query."""
    with services(ctx) as s:
        results = s.retrieval.retrieve(session, query, project, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("context.retrieve", {"query": query, "results": results}))
            return
        if not results:
            typer.echo("No candidates found.")
            return
        for item in results:
            pin_mark = "*" if item["pinned"] else " "
            typer.echo(
                f"{pin_mark}[{item['source']}] {item['label']}  "
                f"(score {item['score']})  {item['detail']}"
            )
        typer.echo("(* = pinned)")


__all__ = ["app"]
