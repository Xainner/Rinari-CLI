"""One-shot work commands (commands.md 13): ask, plan, agent, review, run,
stop, verify.

Each command starts (or reuses) a session, runs the agent loop exactly once
with a purpose-built preamble, prints the result, and ends the session.
Deterministic sub-commands (`stop`, `verify`) never spend a model call.
Read-only commands (`ask`, `plan`, `review`) run under the read-only profile
so they cannot modify the working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from rinari.cli import agent_runtime
from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.policy.engine import PermissionProfile
from rinari.shared.errors import InvalidUsageError, NotFoundError

app = typer.Typer(help="One-shot work commands.", no_args_is_help=True)


def _preamble(command: str, target: str) -> str:
    if command == "ask":
        return (
            "Answer the user's question directly and concisely. You are in "
            "read-only mode: do not modify any files. Use read/search/Git tools "
            f"as needed to ground your answer. The question is:\n\n{target}"
        )
    if command == "plan":
        return (
            "Investigate the codebase and produce a concrete implementation "
            "plan for the request below. Do NOT modify any files. Structure the "
            "plan as: (1) goal, (2) files to create/modify with rationale, "
            "(3) ordered steps, (4) risks, (5) how to verify. The request is:\n\n"
            f"{target}"
        )
    if command == "review":
        return (
            "Review recent changes in this repository (git diff and recently "
            "modified files). Report concrete issues ordered by severity: bugs "
            "first, then security, then conventions. Reference file and line. "
            f"Do NOT modify any files. Focus on:\n\n{target}"
        )
    if command == "agent":
        return (
            "Carry out the autonomous task below end-to-end. You may read and "
            "modify files within the workspace. Validate your work when a "
            "reasonable check exists. The task is:\n\n"
            f"{target}"
        )
    # run
    return f"Execute the following task:\n\n{target}"


def _profile_for(command: str) -> PermissionProfile:
    if command in ("ask", "plan", "review"):
        return PermissionProfile.READ_ONLY
    return PermissionProfile.WORKSPACE


def _one_shot(ctx: typer.Context, command: str, target: list[str] | None) -> None:
    text = " ".join(target).strip() if target else ""
    if not text:
        raise InvalidUsageError(f"`rinari {command}` requires a task/prompt argument")
    with services(ctx) as s:
        started = s.sessions.start(Path.cwd())
        record = started.session
        session = agent_runtime.build_agent_session(
            s, record, interactive=False, profile=_profile_for(command)
        )
        try:
            result = agent_runtime.run_turn(
                session,
                _preamble(command, text),
                on_delta=lambda d: print(d, end="", flush=True),
            )
        finally:
            session.end()
        data = {
            "command": command,
            "session": record.id,
            "kind": result.kind,
            "content": result.content,
            "tool_calls": result.tool_calls,
            "compacted": result.compacted,
            "completion": result.completion,
            "usage": result.usage,
        }
        if is_json(ctx):
            emit_json(success_envelope(f"{command}", data))
            return
        if result.kind in ("answer", "truncated"):
            if result.content:
                typer.echo()
            typer.echo(result.content)
            if result.kind == "truncated":
                typer.echo("(output stopped at the model's max tokens)", err=True)
        else:
            typer.echo(f"[{result.kind}] {result.content}", err=True)
            raise typer.Exit(1 if result.kind not in ("cancelled",) else 130)
        if result.completion:
            typer.echo(_completion_line(result), err=True)
        typer.echo(f"(session {record.id})", err=True)


def _completion_line(result) -> str:
    outcome = (result.completion or {}).get("outcome")
    if not outcome:
        return ""
    reasons = "; ".join((result.completion or {}).get("reasons") or [])
    text = f"completion: {outcome}"
    return f"{text} ({reasons})" if reasons else text


@app.command()
@with_error_handling("ask")
def ask(ctx: typer.Context, target: list[str] = typer.Argument(None)) -> None:
    """Ask about the project without modifying it."""
    _one_shot(ctx, "ask", target)


@app.command()
@with_error_handling("plan")
def plan(ctx: typer.Context, target: list[str] = typer.Argument(None)) -> None:
    """Investigate and produce an implementation plan (no modifications)."""
    _one_shot(ctx, "plan", target)


@app.command()
@with_error_handling("agent")
def agent(ctx: typer.Context, target: list[str] = typer.Argument(None)) -> None:
    """Run an autonomous task in this workspace."""
    _one_shot(ctx, "agent", target)


@app.command()
@with_error_handling("review")
def review(ctx: typer.Context, target: list[str] = typer.Argument(None)) -> None:
    """Review recent changes without modifying them by default."""
    _one_shot(ctx, "review", target)


@app.command()
@with_error_handling("run")
def run(ctx: typer.Context, target: list[str] = typer.Argument(None)) -> None:
    """Run a task explicitly."""
    _one_shot(ctx, "run", target)


@app.command()
@with_error_handling("stop")
def stop(ctx: typer.Context, session: str = typer.Argument(None)) -> None:
    """Stop the latest session's current turn (no model call)."""
    with services(ctx) as s:
        if session:
            record = s.sessions.show(session)
        else:
            records = s.sessions.list(limit=1)
            if not records:
                raise NotFoundError("No sessions to stop")
            record = records[0]
        record.state = "stopped"
        from rinari.shared.clock import now_iso

        record.updated_at = now_iso(s.ctx.clock)
        s.ctx.session_repo.update(record)
        data = {"session": record.id, "state": record.state}
        if is_json(ctx):
            emit_json(success_envelope("stop", data))
            return
        typer.echo(f"session {record.id} marked {record.state}")


@app.command()
@with_error_handling("verify")
def verify(
    ctx: typer.Context,
    command: list[str] = typer.Argument(
        None, help="Verification command to run (default: project test)."
    ),
) -> None:
    """Run a verification command and record the result (no model call)."""
    with services(ctx) as s:
        root = Path.cwd()
        cmd = [c for c in (command or []) if c]
        if not cmd:
            cmd = _default_verify_command(root, s)
        if not cmd:
            raise InvalidUsageError(
                "No verification command given and none detected",
                hint="Pass a command: rinari verify pytest -q",
            )
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600)
        record = s.verification.record(
            root,
            kind="test",
            result="passed" if result.returncode == 0 else "failed",
            command=" ".join(cmd),
            summary=f"exit {result.returncode}",
            detail=(result.stdout or "")[-4000:],
        )
        data = {
            "command": " ".join(cmd),
            "result": "passed" if result.returncode == 0 else "failed",
            "exit": result.returncode,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-4000:],
            "record": record,
        }
        if is_json(ctx):
            emit_json(success_envelope("verify", data))
            return
        stdout = (result.stdout or "").rstrip()
        if stdout:
            typer.echo(stdout)
        stderr = (result.stderr or "").rstrip()
        if stderr:
            typer.echo(stderr, err=True)
        status = "PASS" if result.returncode == 0 else "FAIL"
        typer.echo(f"[{status}] {' '.join(cmd)} (exit {result.returncode})")
        if result.returncode != 0:
            raise typer.Exit(1)


def _default_verify_command(root: Path, s) -> list[str] | None:
    """Detect a standard project test command from its manifest."""
    import sys

    if (root / "pyproject.toml").is_file():
        return [sys.executable, "-m", "pytest", "-q"]
    if (root / "package.json").is_file():
        return ["npm", "test", "--", "--silent"]
    if (root / "Makefile").is_file():
        return ["make", "test"]
    return None
