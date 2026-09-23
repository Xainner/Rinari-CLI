"""`rinari flow`: how a project (or a chat) advanced, stage by stage.

Same projection the desktop shows (`flow.get`, `project_flow_v1`): stages are
contiguous runs of turns in one mode across the sessions of the scope, and
every number comes from persisted facts. Unknown values print as `—`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.engine_protocol.flow import collect_flow
from rinari.shared.errors import InvalidUsageError, NotFoundError

STAGE_LABEL = {"planning": "PLAN", "implementation": "BUILD", "review": "REVIEW"}
STATUS_STYLE = {
    "done": "green",
    "active": "magenta",
    "needs_you": "yellow",
    "failed": "red",
    "stopped": "dim",
}


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


def _duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    seconds = max(0, round(ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def _resolve_project_id(s, project: str) -> str:
    canonical = str(Path(project).expanduser().resolve())
    record = s.ctx.project_repo.get_by_root(canonical)
    if record is None:
        raise NotFoundError(
            f"No registered project at {canonical}",
            hint="Register it first with `rinari project add <path>`.",
        )
    return record.id


@with_error_handling("flow")
def flow(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", help="Registered project directory (default: current)."
    ),
    session_id: str = typer.Option(None, "--session", "-s", help="A single session instead."),
) -> None:
    """Show the stages of a project or of one session."""
    if project and session_id:
        raise InvalidUsageError("Use --project or --session, not both.")
    with services(ctx) as s:
        if session_id:
            payload = collect_flow(s, session_id=session_id)
        else:
            payload = collect_flow(s, project_id=_resolve_project_id(s, project or "."))
        if is_json(ctx):
            emit_json(success_envelope("flow", payload))
            return
        console = Console()
        scope, summary = payload["scope"], payload["summary"]
        console.print(
            f"[bold]{scope['title']}[/bold]  ·  {summary['stages_total']} stages, "
            f"{summary['turns_total']} turns, {summary['files_changed']} files  ·  "
            f"progress {_percent(summary['progress'])}"
        )
        if not payload["stages"]:
            console.print("[dim]No turns yet in this scope.[/dim]")
            return
        table = Table(show_lines=False, pad_edge=False)
        for column in (
            "#",
            "Cycle",
            "Stage",
            "Status",
            "Progress",
            "Turns",
            "Files",
            "Duration",
            "Title",
        ):
            table.add_column(column)
        for stage in payload["stages"]:
            style = STATUS_STYLE.get(stage["status"], "")
            table.add_row(
                str(stage["index"]),
                str(stage["cycle_index"]),
                STAGE_LABEL.get(stage["kind"], stage["kind"]),
                f"[{style}]{stage['status']}[/{style}]" if style else stage["status"],
                _percent(stage["progress"]),
                str(stage["turns"]),
                str(len(stage["files"]) + stage["files_more"]),
                _duration(stage["duration_ms"]),
                stage["title"] or stage["excerpt"] or "—",
            )
        console.print(table)
