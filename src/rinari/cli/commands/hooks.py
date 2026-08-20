"""`rinari hooks` group: lifecycle hooks management (phase 5).

Per docs/commands.md section 48. Untrusted project hooks do not execute;
`list`/`show` reflect that by marking them `skipped (untrusted)`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.hooks import ALL_EVENTS
from rinari.shared.errors import InvalidUsageError, RinariError

app = typer.Typer(help="Manage lifecycle hooks.", no_args_is_help=True)


def _project(ctx: typer.Context) -> Path | None:
    from rinari.projects.detector import detect_project

    return detect_project(Path.cwd(), Path.home()).project_root


@app.command("list")
@with_error_handling("hooks.list")
def hooks_list(ctx: typer.Context) -> None:
    """List hook declarations (user, project, plugins) with their state."""
    with services(ctx) as s:
        project = _project(ctx)
        rows = s.hooks.list(project)
        if is_json(ctx):
            emit_json(success_envelope("hooks.list", {"hooks": rows}))
            return
        if not rows:
            typer.echo("No hooks declared (~/.rinari/hooks.json, .rinari/hooks.json, plugins).")
            return
        typer.echo(f"{'EVENT':<18} {'NAME':<24} {'SOURCE':<10} {'TYPE':<7} ENABLED")
        for row in rows:
            typer.echo(
                f"{row['event']:<18} {row['name']:<24} {row['source']:<10} "
                f"{row['handler_type']:<7} {'yes' if row['enabled'] else 'no'}"
            )


@app.command("show")
@with_error_handling("hooks.show")
def hooks_show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Show one hook declaration and its trust state."""
    with services(ctx) as s:
        project = _project(ctx)
        row = s.hooks.show(name, source, "global" if source != "project" else "project", project)
        if row is None:
            raise RinariError(f"hook not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("hooks.show", row))
            return
        typer.echo(f"name:       {row['name']}")
        typer.echo(f"event:      {row['event']}")
        typer.echo(f"source:     {row['source']}")
        typer.echo(f"type:       {row['handler_type']}")
        typer.echo(f"handler:    {row['handler']}")
        typer.echo(f"capabilities:{', '.join(row['capabilities']) or '(none)'}")
        typer.echo(f"enabled:    {'yes' if row['enabled'] else 'no'}")


@app.command("enable")
@with_error_handling("hooks.enable")
def hooks_enable(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Enable a declared hook."""
    scope = "global" if source != "project" else "project"
    with services(ctx) as s:
        row = s.hooks.enable(name, source, scope)
        if row is None:
            raise RinariError(f"hook not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("hooks.enable", row))
            return
        typer.echo(f"enabled {name}")


@app.command("disable")
@with_error_handling("hooks.disable")
def hooks_disable(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    source: str = typer.Option("user", "--source"),
) -> None:
    """Disable a declared hook without editing its file."""
    scope = "global" if source != "project" else "project"
    with services(ctx) as s:
        row = s.hooks.disable(name, source, scope)
        if row is None:
            raise RinariError(f"hook not found: {name} ({source})")
        if is_json(ctx):
            emit_json(success_envelope("hooks.disable", row))
            return
        typer.echo(f"disabled {name}")


@app.command("test")
@with_error_handling("hooks.test")
def hooks_test(
    ctx: typer.Context,
    event: str = typer.Argument(..., help=f"One of: {', '.join(ALL_EVENTS)}"),
    payload: str = typer.Option("{}", "--payload", help="JSON payload delivered to handlers."),
) -> None:
    """Dry-run an event against the current hooks (shows outcomes)."""
    if event not in ALL_EVENTS:
        raise InvalidUsageError(f"unknown event: {event!r}", hint=f"valid: {', '.join(ALL_EVENTS)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidUsageError(f"--payload must be JSON: {exc}") from None
    with services(ctx) as s:
        outcomes = s.hooks.test(event, data, _project(ctx))
        if is_json(ctx):
            emit_json(success_envelope("hooks.test", {"event": event, "outcomes": outcomes}))
            return
        if not outcomes:
            typer.echo(f"No hooks for {event}.")
            return
        for outcome in outcomes:
            status = "ok" if outcome["ok"] else f"FAIL {outcome['error']}"
            typer.echo(f"{outcome['name']}  {status}")
            if outcome.get("output"):
                typer.echo(f"  {outcome['output'][:200]}")


@app.command("doctor")
@with_error_handling("hooks.doctor")
def hooks_doctor(ctx: typer.Context) -> None:
    """Diagnose hook files, declarations, and trust state."""
    with services(ctx) as s:
        report = s.hooks.doctor(_project(ctx))
        if is_json(ctx):
            emit_json(success_envelope("hooks.doctor", {"report": report}))
            return
        for entry in report:
            if entry.get("file") is None:
                if entry.get("status") == "errors":
                    for diag in entry["diagnostics"]:
                        typer.echo(f"error: {diag['code']}: {diag['message']}")
                elif "project_trusted" in entry:
                    state = entry["project_trusted"]
                    typer.echo(
                        "project hooks: "
                        + ("trusted (they execute)" if state else "NOT trusted (they are skipped)")
                        if state is not None
                        else "project hooks: no project in this directory"
                    )
                continue
            typer.echo(f"{entry['file']}: {entry['status']} ({entry['hooks']} hook(s))")


__all__ = ["app"]
