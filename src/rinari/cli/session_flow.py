"""Shared session start/resume flow for the root command, `chat`, and `resume`.

Phase-2 behavior: detect the context, resolve the active provider + model,
open or resume the compatible session, then hand the conversation to the
agent runtime. Human output goes to a streaming REPL (or a single non-
interactive turn when piped with a prompt); `--json` emits the machine
envelope with the recorded session and, when a prompt was given, the turn
result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from rinari import __version__
from rinari.cli import agent_runtime, repl
from rinari.cli.deps import fail, is_json, services
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import session_dict
from rinari.projects.git import git_state
from rinari.runtime.agent import TurnResult
from rinari.shared.errors import RinariError


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


def _print_header(data: dict, created: bool, warnings: tuple[str, ...]) -> None:
    typer.echo(f"Rinari v{data['version']}  {data['kind']}")
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
    typer.echo(f"Session   {data['session']['id']} ({'created' if created else 'resumed'})")
    for warning in warnings:
        typer.echo(f"warning   {warning}", err=True)


def _reconciliation_dict(findings) -> list[dict]:
    return [
        {"subsystem": f.subsystem, "state": f.state, "detail": f.detail, "action": f.action}
        for f in findings
        if f.state != "ok"
    ]


def _turn_dict(result: TurnResult) -> dict:
    return {
        "kind": result.kind,
        "content": result.content,
        "tool_calls": result.tool_calls,
        "usage": (
            {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            }
            if result.usage is not None
            else None
        ),
        "completion": result.completion,
        "compacted": result.compacted,
        "budget": result.budget,
    }


def _compacted_line(result: TurnResult) -> str | None:
    if not result.compacted:
        return None
    return "context: compacted (history summarized, task state preserved)"


def _completion_line(result: TurnResult) -> str | None:
    if not result.completion:
        return None
    outcome = result.completion.get("outcome")
    if outcome is None:
        return None
    reasons = "; ".join(result.completion.get("reasons") or [])
    text = f"completion: {outcome}"
    return f"{text} ({reasons})" if reasons else text


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def start_flow(ctx: typer.Context, prompt: str | None, forced_chat: bool, command: str) -> None:
    with services(ctx) as s:
        try:
            started = s.sessions.start(Path.cwd(), forced_chat=forced_chat, prompt=prompt)
        except RinariError as err:
            fail(ctx, command, err)
        data = _snapshot(s, started.session)
        data["created"] = started.created
        data["warnings"] = list(started.warnings)
        data["reconciliation"] = _reconciliation_dict(started.findings)
        data["prompt_recorded"] = prompt is not None

        if is_json(ctx):
            if prompt:
                session = agent_runtime.build_agent_session(s, started.session, interactive=False)
                try:
                    data["turn"] = _turn_dict(agent_runtime.run_turn(session, prompt))
                except RinariError as err:
                    fail(ctx, command, err)
                finally:
                    session.end()
                if session.promoted_root is not None:
                    data["promoted_root"] = str(session.promoted_root)
            emit_json(success_envelope(command, data, warnings=started.warnings))
            return

        _print_header(data, started.created, started.warnings)
        session = agent_runtime.build_agent_session(s, started.session, interactive=True)
        if prompt is not None and not _interactive():
            try:
                result = agent_runtime.run_turn(session, prompt)
            finally:
                session.end()
            typer.echo()
            typer.echo(result.content)
            if result.kind not in ("answer", "truncated", "cancelled"):
                typer.echo(f"[{result.kind}]")
            compacted = _compacted_line(result)
            if compacted:
                typer.echo(compacted)
            completion = _completion_line(result)
            if completion:
                typer.echo(completion)
            if session.promoted_root is not None:
                typer.echo(f"*** session promoted to PROJECT — root: {session.promoted_root} ***")
            return
        try:
            repl.run_repl(session, initial_prompt=prompt)
        except RinariError as err:
            fail(ctx, command, err)
        finally:
            session.end()


def resume_flow(ctx: typer.Context, ref: str | None, command: str) -> None:
    with services(ctx) as s:
        try:
            started = s.sessions.resume(ref, cwd=Path.cwd())
        except RinariError as err:
            fail(ctx, command, err)
        data = _snapshot(s, started.session)
        data["resumed_from"] = ref
        data["warnings"] = list(started.warnings)
        data["reconciliation"] = _reconciliation_dict(started.findings)
        if is_json(ctx):
            emit_json(success_envelope(command, data, warnings=started.warnings))
            return
        _print_header(data, created=False, warnings=started.warnings)
        session = agent_runtime.build_agent_session(s, started.session, interactive=True)
        try:
            repl.run_repl(session)
        except RinariError as err:
            fail(ctx, command, err)
        finally:
            session.end()
