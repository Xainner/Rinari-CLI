"""`rinari schedule` group: scheduled tasks.

The tasks run in the Engine of the desktop app (open or in the tray); this
group creates and inspects them in the same state database.
"""

from __future__ import annotations

from datetime import datetime

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.schedule.service import ScheduledTaskError
from rinari.shared.errors import InvalidUsageError, NotFoundError

app = typer.Typer(help="Scheduled tasks (run by the desktop app's Engine).", no_args_is_help=True)


def _when(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def _schedule_from(
    at: str | None, every: int | None, daily: str | None, weekly: str | None
) -> dict:
    chosen = [value is not None for value in (at, every, daily, weekly)]
    if sum(chosen) != 1:
        raise InvalidUsageError(
            "Give exactly one of --at, --every, --daily or --weekly.",
            hint='e.g. --daily 08:30, --weekly "0,2,4@08:30", --every 30',
        )
    if at is not None:
        return {"kind": "once", "at": at}
    if every is not None:
        return {"kind": "interval", "minutes": every}
    if daily is not None:
        return {"kind": "daily", "time": daily}
    assert weekly is not None
    days, _, time = weekly.partition("@")
    try:
        numbers = [int(day) for day in days.split(",") if day.strip()]
    except ValueError:
        raise InvalidUsageError("--weekly is DAYS@HH:MM, days 0 (Monday) to 6.") from None
    return {"kind": "weekly", "days": numbers, "time": time}


def _task(s, task_id: str) -> dict:
    try:
        return s.schedules.get(task_id)
    except KeyError:
        raise NotFoundError(f"Unknown scheduled task: {task_id}") from None


@app.command("list")
@with_error_handling("schedule.list")
def schedule_list(ctx: typer.Context) -> None:
    """List scheduled tasks with their next and last run."""
    with services(ctx) as s:
        tasks = s.schedules.list()
        if is_json(ctx):
            emit_json(success_envelope("schedule.list", tasks))
            return
        if not tasks:
            typer.echo("No scheduled tasks (rinari schedule add ...).")
            return
        for task in tasks:
            state = "on " if task["enabled"] else "off"
            last = task["last_run"]
            last_text = f"last {last['status']}" if last else "never ran"
            typer.echo(
                f"{task['id']}  [{state}] {task['name']} · {task['description']} · "
                f"next {_when(task['next_run_at'])} · {last_text}"
            )


@app.command("add")
@with_error_handling("schedule.add")
def schedule_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Task name."),
    prompt: str = typer.Option(..., "--prompt", help="What Rinari does (or reminds)."),
    reminder: bool = typer.Option(False, "--reminder", help="Only notify, no agent turn."),
    at: str | None = typer.Option(None, "--at", help="Once, local time YYYY-MM-DDTHH:MM."),
    every: int | None = typer.Option(None, "--every", help="Every N minutes (5 or more)."),
    daily: str | None = typer.Option(None, "--daily", help="Every day at HH:MM."),
    weekly: str | None = typer.Option(None, "--weekly", help="DAYS@HH:MM, e.g. 0,2,4@08:30."),
    project: str | None = typer.Option(None, "--project", help="Project id to run in."),
    mode: str = typer.Option("build", "--mode", help="plan, build or review."),
    model: str | None = typer.Option(None, "--model", help="Model alias for the run."),
    allow: list[str] = typer.Option(
        [], "--allow", help="Capability approved in advance (repeatable), e.g. shell.exec."
    ),
) -> None:
    """Create a scheduled task."""
    with services(ctx) as s:
        try:
            task = s.schedules.create(
                {
                    "name": name,
                    "kind": "reminder" if reminder else "agent",
                    "schedule": _schedule_from(at, every, daily, weekly),
                    "prompt": prompt,
                    "project_id": project,
                    "mode": mode,
                    "model": model,
                    "grants": [{"capability": capability} for capability in allow],
                }
            )
        except ScheduledTaskError as exc:
            raise InvalidUsageError(str(exc)) from None
        if is_json(ctx):
            emit_json(success_envelope("schedule.add", task))
            return
        typer.echo(f"{task['id']}  {task['name']} · next {_when(task['next_run_at'])}")


def _set_enabled(ctx: typer.Context, task_id: str, enabled: bool, command: str) -> None:
    with services(ctx) as s:
        _task(s, task_id)
        task = s.schedules.update(task_id, {"enabled": enabled})
        if is_json(ctx):
            emit_json(success_envelope(command, task))
            return
        typer.echo(f"{task['name']}: {'on' if enabled else 'off'}")


@app.command("enable")
@with_error_handling("schedule.enable")
def schedule_enable(ctx: typer.Context, task_id: str = typer.Argument(...)) -> None:
    """Switch a task back on (it counts from now)."""
    _set_enabled(ctx, task_id, True, "schedule.enable")


@app.command("disable")
@with_error_handling("schedule.disable")
def schedule_disable(ctx: typer.Context, task_id: str = typer.Argument(...)) -> None:
    """Switch a task off without deleting it."""
    _set_enabled(ctx, task_id, False, "schedule.disable")


@app.command("remove")
@with_error_handling("schedule.remove")
def schedule_remove(ctx: typer.Context, task_id: str = typer.Argument(...)) -> None:
    """Delete a task and its history."""
    with services(ctx) as s:
        _task(s, task_id)
        s.schedules.delete(task_id)
        if is_json(ctx):
            emit_json(success_envelope("schedule.remove", {"task_id": task_id}))
            return
        typer.echo(f"Removed {task_id}.")


@app.command("runs")
@with_error_handling("schedule.runs")
def schedule_runs(
    ctx: typer.Context,
    task_id: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """Show a task's recent runs."""
    with services(ctx) as s:
        _task(s, task_id)
        runs = s.ctx.schedule_repo.runs(task_id, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("schedule.runs", runs))
            return
        if not runs:
            typer.echo("No runs yet.")
            return
        for run in runs:
            when = _when(run["started_at"] or run["scheduled_for"])
            detail = run["reason"] or (run["summary"] or "").splitlines()[0:1]
            text = detail if isinstance(detail, str) else " ".join(detail)
            session = f" · {run['session_id']}" if run["session_id"] else ""
            typer.echo(f"{when}  {run['status']:<9} {text}{session}".rstrip())
