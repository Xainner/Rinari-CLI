"""Top-level `rinari export` / `rinari import` (commands.md 55/56): session
documents. `export` defaults to the most recent session.
"""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.serializers import session_dict
from rinari.cli.session_export import export_session, import_session
from rinari.shared.errors import InvalidUsageError

from .sessions import _json

export_app = typer.Typer()  # bare command, no subgroups


@export_app.command()
@with_error_handling("export")
def export(
    ctx: typer.Context,
    ref: str = typer.Argument(None, help="Session ID / prefix (default: most recent)."),
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export a session document (JSON)."""
    with services(ctx) as s:
        document = export_session(s, ref)
    if out:
        from pathlib import Path

        Path(out).write_text(_json(document), encoding="utf-8")
        if is_json(ctx):
            emit_json(
                success_envelope("export", {"session": document["session"]["id"], "path": out})
            )
        else:
            typer.echo(f"exported {document['session']['id']} -> {out}")
        return
    typer.echo(_json(document))


import_app = typer.Typer()


@import_app.command()
@with_error_handling("import")
def import_(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Session document file."),
) -> None:
    """Import a session document."""
    from pathlib import Path

    if not Path(path).is_file():
        raise InvalidUsageError(f"File not found: {path}")
    with services(ctx) as s:
        record = import_session(s, Path(path).read_text(encoding="utf-8"))
        if is_json(ctx):
            emit_json(success_envelope("import", session_dict(record)))
            return
        typer.echo(f"imported {record.id} ({record.kind})")


from rinari.cli.output import emit_json, success_envelope  # noqa: E402
