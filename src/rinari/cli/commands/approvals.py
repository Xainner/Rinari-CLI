"""`rinari approvals` group: grants management + consent history (commands.md 38)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.policy.approval_store import ApprovalStore, store_path_for
from rinari.shared.errors import InvalidUsageError, NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Approval grants and history.", no_args_is_help=True)


def _store(s) -> ApprovalStore:
    return ApprovalStore(store_path_for(s.ctx.layout))


def _row(key: str, grant) -> dict:
    return {
        "key": key,
        "capability": grant.capability,
        "target": grant.target,
        "scope": grant.scope.value,
        "session_id": grant.session_id,
        "project_id": grant.project_id,
        "granted_at": grant.granted_at,
    }


@app.command("list")
@with_error_handling("approvals.list")
def list_cmd(ctx: typer.Context) -> None:
    """List persistent approval grants."""
    with services(ctx) as s:
        rows = [_row(k, g) for k, g in sorted(_store(s).load().items())]
        if is_json(ctx):
            emit_json(success_envelope("approvals.list", rows))
            return
        if not rows:
            typer.echo("no persistent grants")
            return
        for row in rows:
            target = f" {row['target']}" if row["target"] else ""
            typer.echo(f"  {row['key']:<40} {row['capability']}{target}  [{row['scope']}]")


@app.command("show")
@with_error_handling("approvals.show")
def show(
    ctx: typer.Context, key: str = typer.Argument(None), capability: str = typer.Argument(None)
) -> None:
    """Show one grant by key (or all grants for a capability)."""
    with services(ctx) as s:
        loaded = _store(s).load()
        if key is not None:
            if key not in loaded:
                raise NotFoundError(f"Grant not found: {key}")
            rows = [_row(key, loaded[key])]
        elif capability is not None:
            rows = [_row(k, g) for k, g in loaded.items() if g.capability == capability]
            if not rows:
                raise NotFoundError(f"No grants for capability: {capability}")
        else:
            raise InvalidUsageError(
                "Provide a grant key or capability", hint="See `rinari approvals list`."
            )
        if is_json(ctx):
            emit_json(success_envelope("approvals.show", rows))
            return
        for row in rows:
            typer.echo(
                f"{row['key']}  {row['capability']}  target={row['target'] or '-'} "
                f"scope={row['scope']}  granted_at={row['granted_at']}"
            )


@app.command("revoke")
@with_error_handling("approvals.revoke")
def revoke(ctx: typer.Context, key: str = typer.Argument(...)) -> None:
    """Revoke one persistent grant by key."""
    with services(ctx) as s:
        store = _store(s)
        loaded = store.load()
        if key not in loaded:
            raise NotFoundError(f"Grant not found: {key}")
        del loaded[key]
        store.save(loaded)
        if is_json(ctx):
            emit_json(success_envelope("approvals.revoke", {"key": key}))
        else:
            typer.echo(f"revoked {key}")


@app.command("clear")
@with_error_handling("approvals.clear")
def clear(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Clear all persistent grants."""
    with services(ctx) as s:
        store = _store(s)
        if not yes:
            answer = typer.prompt("Clear ALL persistent grants?", default="n").lower()
            if answer not in ("y", "yes"):
                typer.echo("cancelled")
                return
        store.save({})
        if is_json(ctx):
            emit_json(success_envelope("approvals.clear", {}))
        else:
            typer.echo("all persistent grants cleared")


@app.command("history")
@with_error_handling("approvals.history")
def history(
    ctx: typer.Context,
    session_id: str = typer.Argument(None),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Show recent approval decisions from session events."""
    with services(ctx) as s:
        if session_id:
            record = s.ctx.session_repo.get(session_id)
            if record is None:
                raise NotFoundError(f"Session not found: {session_id}")
        else:
            candidates = s.ctx.session_repo.list(limit=1)
            if not candidates:
                raise NotFoundError("No sessions found")
            record = candidates[0]
        events = s.ctx.event_repo.list(record.id)
        rows = [
            {
                "seq": e.seq,
                "type": e.type,
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in events
            if e.type in ("PermissionRequest", "ToolApproved", "ApprovalDenied")
        ][-limit:]
        data = {"session": record.id, "events": rows}
        if is_json(ctx):
            emit_json(success_envelope("approvals.history", data))
            return
        if not rows:
            typer.echo(f"no approval events for session {record.id}")
            return
        for row in rows:
            payload = row["payload"] or {}
            detail = payload.get("capability") or payload.get("tool") or ""
            typer.echo(f"  #{row['seq']:<4} {row['type']} {detail}")
