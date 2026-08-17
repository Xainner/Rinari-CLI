"""`rinari tasks` group: externalized task graph (phase 3)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.tasks import core

app = typer.Typer(help="Externalized task graph.", no_args_is_help=True)

ProjectOpt = typer.Option(".", "--project", help="Project directory.")

_STATUS_WIDTH = 10


def _print_task_line(task: dict, depth: int = 0) -> None:
    pad = "  " * depth
    typer.echo(f"{pad}[{task['status']:<{_STATUS_WIDTH - 1}}] {task['title']}")


def _print_done_when(report: dict) -> None:
    for group in ("acceptance", "implementation", "validation", "scope"):
        info = report[group]
        state = "ok" if info["total"] and info["ok"] else "no" if info["total"] else "-"
        typer.echo(f"  {group:<14} {info['satisfied']}/{info['total']}  [{state}]")
    if report["unresolved"]:
        typer.echo(f"  unresolved      {len(report['unresolved'])} outstanding")
    typer.echo(f"  can complete: {'yes' if report['can_complete'] else 'no'}")


@app.command("list")
@with_error_handling("tasks.list")
def tasks_list(ctx: typer.Context, project: str = ProjectOpt) -> None:
    """List tasks for a project."""
    with services(ctx) as s:
        tasks = s.tasks.list(project)
        if is_json(ctx):
            emit_json(success_envelope("tasks.list", tasks))
            return
        if not tasks:
            typer.echo("No tasks yet (rinari tasks add <title>).")
            return
        depths = core.tree_depths(tasks)
        for task in tasks:
            _print_task_line(task, depths[task["id"]])


@app.command("show")
@with_error_handling("tasks.show")
def tasks_show(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task ID."),
    project: str = ProjectOpt,
) -> None:
    """Show a task with its done-when report."""
    with services(ctx) as s:
        task = s.tasks.show(project, task_id)
        report = task.pop("done_when")
        if is_json(ctx):
            emit_json(success_envelope("tasks.show", {**task, "done_when": report}))
            return
        typer.echo(f"{task['id']}  [{task['status']}]  {task['title']}")
        if task["description"]:
            typer.echo(f"  {task['description']}")
        if task["depends_on"]:
            typer.echo(f"  depends on:   {task['depends_on']}")
        if task["blockers"]:
            typer.echo(f"  blocker:      {task['blockers']}")
        if task["evidence"]:
            typer.echo(f"  evidence:     {task['evidence']}")
        _print_done_when(report)


@app.command("tree")
@with_error_handling("tasks.tree")
def tasks_tree(ctx: typer.Context, project: str = ProjectOpt) -> None:
    """Show the task graph as an indented tree."""
    with services(ctx) as s:
        graph = s.tasks.tree(project)
        if is_json(ctx):
            emit_json(success_envelope("tasks.tree", graph))
            return
        if not graph["tasks"]:
            typer.echo("No tasks yet.")
            return
        for task in graph["tasks"]:
            _print_task_line(task, graph["depths"][task["id"]])


@app.command("add")
@with_error_handling("tasks.add")
def tasks_add(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Task title."),
    project: str = ProjectOpt,
    description: str = typer.Option("", "--description", "-d"),
    acceptance: str = typer.Option("", "--acceptance", help="Checklist: '- [ ] ...' lines."),
    implementation: str = typer.Option("", "--implementation"),
    validation: str = typer.Option("", "--validation"),
    scope: str = typer.Option("", "--scope"),
    unresolved: str = typer.Option("", "--unresolved"),
    depends_on: str = typer.Option("", "--depends-on", help="Comma-separated task IDs."),
) -> None:
    """Add a task to the project graph."""
    with services(ctx) as s:
        task = s.tasks.add(
            project,
            title,
            description=description,
            acceptance=acceptance,
            implementation=implementation,
            validation=validation,
            scope=scope,
            unresolved=unresolved,
            depends_on=depends_on,
        )
        if is_json(ctx):
            emit_json(success_envelope("tasks.add", task))
            return
        typer.echo(f"Added {task['id']}: {task['title']}")


@app.command("update")
@with_error_handling("tasks.update")
def tasks_update(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task ID."),
    project: str = ProjectOpt,
    title: str = typer.Option(None, "--title", "-t"),
    description: str = typer.Option(None, "--description", "-d"),
    status: str = typer.Option(None, "--status"),
    blocker: str = typer.Option(None, "--blocker"),
    acceptance: str = typer.Option(None, "--acceptance"),
    implementation: str = typer.Option(None, "--implementation"),
    validation: str = typer.Option(None, "--validation"),
    scope: str = typer.Option(None, "--scope"),
    unresolved: str = typer.Option(None, "--unresolved"),
    depends_on: str = typer.Option(None, "--depends-on"),
    evidence: str = typer.Option(None, "--evidence", help="Append a semicolon-separated ref."),
) -> None:
    """Update task fields; `--status` enforces the status machine."""
    with services(ctx) as s:
        result = s.tasks.update(
            project,
            task_id,
            title=title,
            description=description,
            status=status,
            blocker=blocker,
            acceptance=acceptance,
            implementation=implementation,
            validation=validation,
            scope=scope,
            unresolved=unresolved,
            depends_on=depends_on,
        )
        if evidence is not None:
            result = s.tasks.resolve_blocker(project, task_id, evidence)
        if is_json(ctx):
            emit_json(success_envelope("tasks.update", result))
            return
        typer.echo(f"Updated {task_id}: {result['title']}  [{result['status']}]")


@app.command("cancel")
@with_error_handling("tasks.cancel")
def tasks_cancel(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task ID."),
    project: str = ProjectOpt,
) -> None:
    """Cancel a task."""
    with services(ctx) as s:
        task = s.tasks.cancel(project, task_id)
        if is_json(ctx):
            emit_json(success_envelope("tasks.cancel", task))
            return
        typer.echo(f"Cancelled {task_id}")


@app.command("retry")
@with_error_handling("tasks.retry")
def tasks_retry(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task ID."),
    project: str = ProjectOpt,
) -> None:
    """Reset a blocked/cancelled task to pending (clears the blocker)."""
    with services(ctx) as s:
        task = s.tasks.retry(project, task_id)
        if is_json(ctx):
            emit_json(success_envelope("tasks.retry", task))
            return
        typer.echo(f"Retried {task_id}: pending (blocker cleared)")


@app.command("blockers")
@with_error_handling("tasks.blockers")
def tasks_blockers(ctx: typer.Context, project: str = ProjectOpt) -> None:
    """Show tasks that are blocked or waiting on dependencies."""
    with services(ctx) as s:
        items = s.tasks.blockers(project)
        if is_json(ctx):
            emit_json(success_envelope("tasks.blockers", items))
            return
        if not items:
            typer.echo("No blockers.")
            return
        for item in items:
            waiting = f" waiting on: {', '.join(item['waiting_on'])}" if item["waiting_on"] else ""
            blocker = f"  ({item['blocker']})" if item["blocker"] else ""
            typer.echo(f"[{item['status']:<9}] {item['id']}  {item['title']}{waiting}{blocker}")
