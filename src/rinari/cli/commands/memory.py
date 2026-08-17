"""`rinari memory`: inspect and manage durable memory stores (phase 4)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Inspect and manage durable memory (user, project, episodic, pattern).")

_SCOPES = ("user", "project", "pattern", "episodic")


def _emit(ctx: typer.Context, command: str, data, *, line: str | None = None) -> None:
    if is_json(ctx):
        emit_json(success_envelope(command, data))
        return
    typer.echo(line if line is not None else "")


def _format_row(row: dict, *, scope: str) -> str:
    bits = [f"[{scope}]", f"({row.get('kind', '')})" if row.get("kind") else f"({scope})"]
    if row.get("topic"):
        bits.append(f"#{row['topic']}")
    text = row.get("text") or row.get("summary") or ""
    bits.append(text)
    if row.get("id"):
        bits.append(row["id"])
    if row.get("updated_at"):
        bits.append(row["updated_at"])
    return "  ".join(bits)


@app.command("list")
@with_error_handling("memory.list")
def memory_list(
    ctx: typer.Context,
    scope: str = typer.Option(None, "--scope", help="user | project | pattern | episodic."),
    project: str = typer.Option(None, "--project", help="Project root (project/episodic)."),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """List memory records (all scopes when --scope is omitted)."""
    if scope is not None and scope not in _SCOPES:
        typer.echo(f"invalid scope: {scope}", err=True)
        raise typer.Exit(2)
    with services(ctx) as s:
        memory = s.memory
        scopes = [scope] if scope else list(_SCOPES)
        rows: dict[str, list[dict]] = {}
        for current in scopes:
            if current == "user":
                rows[current] = memory.list_user(limit=limit)
            elif current == "project":
                if not project:
                    rows[current] = []
                    continue
                rows[current] = memory.list_project(project, limit=limit)
            elif current == "pattern":
                rows[current] = memory.list_pattern(limit=limit)
            else:
                rows[current] = memory.list_episodic(project or None, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("memory.list", rows))
            return
        total = sum(len(v) for v in rows.values())
        if not total:
            typer.echo("No memory records.")
            return
        for current in scopes:
            current_rows = rows[current]
            typer.echo(f"== {current} ({len(current_rows)}) ==")
            for row in current_rows:
                typer.echo(f"  {_format_row(row, scope=current)}")


@app.command("search")
@with_error_handling("memory.search")
def memory_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    scope: str = typer.Option("user", "--scope", help="user | project | pattern | episodic."),
    project: str = typer.Option(None, "--project", help="Project root (project/episodic)."),
    kind: str = typer.Option(None, "--kind", help="Filter by kind."),
    limit: int = typer.Option(10, "--limit", min=1, max=100),
) -> None:
    """Search memory records by text/topic."""
    with services(ctx) as s:
        memory = s.memory
        if scope == "user":
            rows = memory.search_user(query, kind=kind, limit=limit)
        elif scope == "project":
            if not project:
                typer.echo("--project is required for project scope", err=True)
                raise typer.Exit(2)
            rows = memory.search_project(project, query, kind=kind, limit=limit)
        elif scope == "pattern":
            rows = memory.search_pattern(query, limit=limit)
        elif scope == "episodic":
            rows = memory.search_episodic(project or "", query, limit=limit)
        else:
            typer.echo(f"invalid scope: {scope}", err=True)
            raise typer.Exit(2)
        _emit(
            ctx,
            "memory.search",
            rows,
            line=f"{len(rows)} match(es)."
            if not rows
            else "\n".join(f"  {_format_row(r, scope=scope)}" for r in rows),
        )


@app.command("show")
@with_error_handling("memory.show")
def memory_show(
    ctx: typer.Context,
    memory_id: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope", help="user | project."),
    project: str = typer.Option(None, "--project", help="Project root (project scope)."),
) -> None:
    """Show one memory record (user or project scope requires --scope)."""
    with services(ctx) as s:
        memory = s.memory
        row = None
        if scope == "user":
            row = memory.get_user(memory_id)
        elif scope == "project":
            if not project:
                typer.echo("--project is required for project scope", err=True)
                raise typer.Exit(2)
            row = memory.repo.project_get(project, memory_id)
        else:
            typer.echo("show supports --scope user|project only", err=True)
            raise typer.Exit(2)
        if row is None:
            typer.echo(f"memory record not found: {memory_id}", err=True)
            raise typer.Exit(3)
        _emit(ctx, "memory.show", row, line="\n".join(f"{k}: {v}" for k, v in row.items()))


@app.command("add")
@with_error_handling("memory.add")
def memory_add(
    ctx: typer.Context,
    text: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope", help="user | project | pattern."),
    kind: str = typer.Option(None, "--kind", help="kind for user/project scope."),
    topic: str = typer.Option(..., "--topic", help="Short normalized topic key."),
    pattern_scope: str = typer.Option(None, "--pattern-scope", help="global | user."),
    provenance: str = typer.Option("cli", "--provenance"),
    confidence: float = typer.Option(1.0, "--confidence", min=0.0, max=1.0),
    project: str = typer.Option(None, "--project", help="Project root (project scope)."),
) -> None:
    """Add one memory record (explicit and durable)."""
    with services(ctx) as s:
        memory = s.memory
        try:
            if scope == "user":
                result = memory.remember_user(
                    text,
                    kind=kind or "preference",
                    topic=topic,
                    provenance=provenance,
                    confidence=confidence,
                )
            elif scope == "project":
                if not project:
                    typer.echo("--project is required for project scope", err=True)
                    raise typer.Exit(2)
                result = memory.remember_project(
                    project,
                    text,
                    kind=kind or "fact",
                    topic=topic,
                    provenance=provenance,
                    confidence=confidence,
                )
            elif scope == "pattern":
                result = memory.remember_pattern(
                    topic, text, scope=pattern_scope or "global", provenance=provenance
                )
            else:
                typer.echo(f"invalid scope: {scope}", err=True)
                raise typer.Exit(2)
        except Exception as exc:
            typer.echo(getattr(exc, "message", str(exc)), err=True)
            raise typer.Exit(2) from exc
        _emit(
            ctx,
            "memory.add",
            result,
            line=f"{result['action']}: {result.get('id')} (scope={scope}, topic={topic})",
        )


@app.command("edit")
@with_error_handling("memory.edit")
def memory_edit(
    ctx: typer.Context,
    memory_id: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope", help="user | project."),
    text: str = typer.Option(None, "--text"),
    topic: str = typer.Option(None, "--topic"),
    confidence: float = typer.Option(None, "--confidence", min=0.0, max=1.0),
    provenance: str = typer.Option(None, "--provenance"),
    project: str = typer.Option(None, "--project", help="Project root (project scope)."),
) -> None:
    """Edit one memory record (at least one field is required)."""
    with services(ctx) as s:
        memory = s.memory
        if text is None and topic is None and confidence is None and provenance is None:
            typer.echo(
                "nothing to update: pass --text, --topic, --confidence or --provenance", err=True
            )
            raise typer.Exit(2)
        try:
            if scope == "user":
                row = memory.update_user(
                    memory_id,
                    text=text,
                    topic=topic,
                    confidence=confidence,
                    provenance=provenance,
                )
            elif scope == "project":
                if not project:
                    typer.echo("--project is required for project scope", err=True)
                    raise typer.Exit(2)
                row = memory.update_project(
                    project,
                    memory_id,
                    text=text,
                    topic=topic,
                    confidence=confidence,
                    provenance=provenance,
                )
            else:
                typer.echo("edit supports --scope user|project only", err=True)
                raise typer.Exit(2)
        except Exception as exc:
            typer.echo(getattr(exc, "message", str(exc)), err=True)
            raise typer.Exit(2) from exc
        _emit(ctx, "memory.edit", row, line=f"updated {memory_id}")


@app.command("forget")
@with_error_handling("memory.forget")
def memory_forget(
    ctx: typer.Context,
    memory_id: str = typer.Argument(...),
    scope: str = typer.Option(
        ..., "--scope", help="user | project | pattern (explicit scope required)."
    ),
    project: str = typer.Option(None, "--project", help="Project root (project scope)."),
) -> None:
    """Forget (delete) one memory record. Deletion always requires an explicit scope."""
    with services(ctx) as s:
        memory = s.memory
        try:
            if scope == "user":
                forgotten = memory.forget_user(memory_id)
            elif scope == "project":
                if not project:
                    typer.echo("--project is required for project scope", err=True)
                    raise typer.Exit(2)
                forgotten = memory.forget_project(project, memory_id)
            elif scope == "pattern":
                forgotten = memory.forget_pattern(memory_id)
            else:
                typer.echo("forget supports --scope user|project|pattern only", err=True)
                raise typer.Exit(2)
        except Exception as exc:
            typer.echo(getattr(exc, "message", str(exc)), err=True)
            raise typer.Exit(2) from exc
        _emit(
            ctx,
            "memory.forget",
            {"scope": scope, "id": memory_id, "forgotten": forgotten},
            line=f"forgot {memory_id}" if forgotten else f"memory record not found: {memory_id}",
        )


__all__ = ["app"]
