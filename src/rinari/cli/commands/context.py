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


@app.command("compact")
@with_error_handling("context.compact")
def context_compact(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """Compact an existing conversation without resuming its task."""
    from rinari.cli.agent_runtime import build_agent_session, compact_session

    with services(ctx) as s:
        record = s.sessions.show(session)
        runtime = build_agent_session(s, record, interactive=not is_json(ctx))
        try:
            result = compact_session(runtime)
            if is_json(ctx):
                emit_json(success_envelope("context.compact", result))
        finally:
            runtime.end()


@app.command("settings")
@with_error_handling("context.settings")
def context_settings(
    ctx: typer.Context,
    summarizer: str | None = typer.Option(
        None, "--summarizer", help="Saved model or 'conversation'."
    ),
    model: str | None = typer.Option(None, "--model"),
    window: int | None = typer.Option(None, "--window", help="0 restores automatic detection."),
    threshold: int | None = typer.Option(None, "--threshold"),
) -> None:
    """Inspect or configure the shared context policy."""
    from rinari.context.settings import load, save

    with services(ctx) as s:
        value = load(s.ctx)
        if summarizer is not None:
            value["model_id"] = (
                None if summarizer == "conversation" else s.models.resolve(summarizer).id
            )
        if window is not None:
            if model is None:
                raise typer.BadParameter("--window requires --model")
            ref = s.models.resolve(model).id
            if window == 0:
                value["model_windows"].pop(ref, None)
            else:
                value["model_windows"][ref] = window
        if threshold is not None:
            value["compact_at_percent"] = threshold
        if any(v is not None for v in (summarizer, window, threshold)):
            value = save(s, value)
        if is_json(ctx):
            emit_json(success_envelope("context.settings", value))
        else:
            typer.echo(
                f"Automatic compaction: {value['enabled']} · "
                f"threshold: {value['compact_at_percent']}%"
            )
            typer.echo(f"Summarizer: {value['model_id'] or 'conversation model'}")
            for ref, size in value["model_windows"].items():
                typer.echo(f"{ref}: {size} tokens (manual)")
