"""Shared session start/resume flow for the root command, `chat`, and `resume`.

Phase-1 behavior: detect the context, resolve the active provider + model,
open or resume the compatible session, persist a prompt event, and print a
runtime snapshot. The conversational agent loop arrives in phase 2 without
changing this flow's contract.
"""

from __future__ import annotations

from pathlib import Path

import typer

from rinari import __version__
from rinari.cli.deps import fail, is_json, services
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import session_dict
from rinari.projects.git import git_state
from rinari.shared.errors import RinariError

NOTES_PHASE_2 = "Agent runtime not available yet (phase 2); the prompt was recorded."


def _snapshot(s, session) -> dict:
    """Gather the displayable state for a session (never invents metrics)."""
    provider = s.ctx.provider_repo.get(session.provider_id)
    model = s.ctx.model_repo.get(session.model_id)
    data: dict = {
        "version": __version__,
        "kind": session.kind,
        "session": session_dict(session),
        "provider": (
            {"id": provider.id, "alias": provider.alias, "type": provider.type}
            if provider is not None
            else None
        ),
        "model": (
            {
                "id": model.id,
                "alias": model.alias,
                "provider_model_id": model.provider_model_id,
            }
            if model is not None
            else None
        ),
        "project_root": session.project_root_snapshot,
        "cwd": session.current_cwd,
    }
    if session.kind == "PROJECT" and session.project_root_snapshot:
        state = git_state(Path(session.project_root_snapshot))
        data["git"] = {"branch": state.branch, "dirty": state.dirty} if state.available else None
    return data


def _print_human(data: dict, created: bool, warnings: tuple[str, ...], prompt: str | None) -> None:
    typer.echo(f"Rinari v{data['version']}  {data['kind']}")
    typer.echo("-" * 40)
    if data["kind"] == "PROJECT" and data.get("project_root"):
        typer.echo(f"Project   {data['project_root']}")
        git = data.get("git")
        if git is not None:
            branch = git["branch"] or "-"
            typer.echo(f"Git       {branch}{' *' if git['dirty'] else ''}")
    else:
        typer.echo(f"cwd       {data['cwd']}")
    provider = data["provider"] or {}
    model = data["model"] or {}
    typer.echo(f"Provider  {provider.get('alias') or '-'} ({provider.get('type') or '-'})")
    if model:
        typer.echo(
            f"Model     {model.get('alias') or '-'} ({model.get('provider_model_id') or '-'})"
        )
    else:
        typer.echo("Model     -")
    typer.echo(f"Session   {data['session']['id']} ({'created' if created else 'resumed'})")
    for warning in warnings:
        typer.echo(f"warning   {warning}", err=True)
    if prompt:
        typer.echo(f"Prompt    {prompt!r} - {NOTES_PHASE_2}")
    else:
        typer.echo(NOTES_PHASE_2)


def start_flow(ctx: typer.Context, prompt: str | None, forced_chat: bool, command: str) -> None:
    with services(ctx) as s:
        try:
            started = s.sessions.start(Path.cwd(), forced_chat=forced_chat, prompt=prompt)
        except RinariError as err:
            fail(ctx, command, err)
        data = _snapshot(s, started.session)
        data["created"] = started.created
        data["warnings"] = list(started.warnings)
        data["prompt_recorded"] = prompt is not None
        if is_json(ctx):
            emit_json(success_envelope(command, data, warnings=started.warnings))
            return
        _print_human(data, started.created, started.warnings, prompt)


def resume_flow(ctx: typer.Context, ref: str | None, command: str) -> None:
    with services(ctx) as s:
        try:
            started = s.sessions.resume(ref, cwd=Path.cwd())
        except RinariError as err:
            fail(ctx, command, err)
        data = _snapshot(s, started.session)
        data["resumed_from"] = ref
        data["warnings"] = list(started.warnings)
        if is_json(ctx):
            emit_json(success_envelope(command, data, warnings=started.warnings))
            return
        _print_human(data, created=False, warnings=started.warnings, prompt=None)
