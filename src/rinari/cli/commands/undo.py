"""`rinari undo` group: checkpoints of agent-owned changes (phase 3).

Undo is ownership-aware: it restores agent-owned paths only. User-owned
(pre-session) paths are never touched, and mixed-ownership paths are
skipped unless `--allow-mixed` is passed (mixed ownership detection).
"""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Undo agent changes via checkpoints.")

ProjectOpt = typer.Option(".", "--project", help="Project directory.")
SessionOpt = typer.Option(None, "--session", help="Session ID (default: latest for project).")
CheckpointOpt = typer.Option(None, "--checkpoint", help="Checkpoint ID (default: latest).")


@app.callback(invoke_without_command=True)
@with_error_handling("undo")
def _undo_default(ctx: typer.Context, project: str = ProjectOpt) -> None:
    """Bare `rinari undo` restores the latest checkpoint (agent-owned paths)."""
    if ctx.invoked_subcommand is not None:
        return
    with services(ctx) as s:
        result = s.checkpoints.restore(project, checkpoint_id=None, preview=False)
        if is_json(ctx):
            emit_json(success_envelope("undo", result))
            return
        typer.echo(f"Restored {result['checkpoint_id']}: {len(result['applied'])} path(s).")
        for path in result["applied"]:
            typer.echo(f"  restored  {path}")
        for item in result["skipped"]:
            typer.echo(f"  skipped   {item['path']}  ({item['reason']})")


def _print_ops(operations: list[dict]) -> None:
    for op in operations:
        action = op["action"]
        typer.echo(f"[{action:<8}] {op['path']}  ({op['reason']})")


@app.command("create")
@with_error_handling("undo.create")
def undo_create(
    ctx: typer.Context,
    project: str = ProjectOpt,
    label: str = typer.Option("", "--label", "-l"),
    session: str = SessionOpt,
) -> None:
    """Create a checkpoint of the current dirty tree."""
    with services(ctx) as s:
        result = s.checkpoints.create(project, label=label, session_id=session)
        if is_json(ctx):
            emit_json(success_envelope("undo.create", result))
            return
        files = [f for f in result["files"] if f["ownership"] == "agent"]
        typer.echo(
            f"Checkpoint {result['id']}: {result['agent_changes']} agent, "
            f"{result['user_owned']} user-owned path(s)"
        )
        for entry in files[:20]:
            typer.echo(f"  [agent] {entry['path']}")
        if len(files) > 20:
            typer.echo(f"  ... and {len(files) - 20} more")


@app.command("list")
@with_error_handling("undo.list")
def undo_list(ctx: typer.Context, project: str = ProjectOpt) -> None:
    """List checkpoints for a project (newest first)."""
    with services(ctx) as s:
        items = s.checkpoints.list(project)
        if is_json(ctx):
            emit_json(success_envelope("undo.list", items))
            return
        if not items:
            typer.echo("No checkpoints yet (rinari undo create).")
            return
        for item in items:
            label = f"  {item['label']}" if item["label"] else ""
            typer.echo(
                f"{item['id']}  {item['created_at']}  "
                f"(agent {item['agent_changes']}, user {item['user_owned']}){label}"
            )


@app.command("preview")
@with_error_handling("undo.preview")
def undo_preview(
    ctx: typer.Context,
    project: str = ProjectOpt,
    checkpoint: str = CheckpointOpt,
    allow_mixed: bool = typer.Option(False, "--allow-mixed"),
) -> None:
    """Preview what `rinari undo` would change (no writes)."""
    with services(ctx) as s:
        result = s.checkpoints.restore(
            project, checkpoint_id=checkpoint, preview=True, allow_mixed=allow_mixed
        )
        if is_json(ctx):
            emit_json(success_envelope("undo.preview", result))
            return
        typer.echo(f"Checkpoint {result['checkpoint_id']} (preview):")
        _print_ops(result["operations"])


@app.command("restore")
@with_error_handling("undo.restore")
def undo_restore(
    ctx: typer.Context,
    project: str = ProjectOpt,
    checkpoint: str = CheckpointOpt,
    allow_mixed: bool = typer.Option(False, "--allow-mixed"),
) -> None:
    """Restore a checkpoint (default: latest). Agent-owned paths only."""
    with services(ctx) as s:
        result = s.checkpoints.restore(
            project, checkpoint_id=checkpoint, preview=False, allow_mixed=allow_mixed
        )
        if is_json(ctx):
            emit_json(success_envelope("undo.restore", result))
            return
        typer.echo(f"Restored {result['checkpoint_id']}: {len(result['applied'])} path(s).")
        for path in result["applied"]:
            typer.echo(f"  restored  {path}")
        for item in result["skipped"]:
            typer.echo(f"  skipped   {item['path']}  ({item['reason']})")


@app.command("remove")
@with_error_handling("undo.remove")
def undo_remove(
    ctx: typer.Context,
    checkpoint_id: str = typer.Argument(..., help="Checkpoint ID."),
    project: str = ProjectOpt,
) -> None:
    """Remove a checkpoint (does not touch the working tree)."""
    with services(ctx) as s:
        removed = s.checkpoints.remove(checkpoint_id)
        if is_json(ctx):
            emit_json(success_envelope("undo.remove", {"id": checkpoint_id, "removed": removed}))
            return
        if not removed:
            typer.echo(f"Checkpoint not found: {checkpoint_id}")
        else:
            typer.echo(f"Removed {checkpoint_id}")
