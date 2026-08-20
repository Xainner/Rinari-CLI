"""`rinari sandbox` group: inspect and test the filesystem sandbox (commands.md 39)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits

from ..output import emit_json, success_envelope

app = typer.Typer(help="Inspect and test the sandbox.", no_args_is_help=True)

PROFILES = {p.value: p for p in PermissionProfile}


def _sandbox_for_path(s, project: str | None) -> FilesystemSandbox:
    from rinari.application.session_service import detect_project

    if project:
        root = Path(project).resolve()
    else:
        detection = detect_project(Path.cwd(), Path.home())
        root = detection.project_root or Path.cwd().resolve()
    home = Path.home().resolve()
    write_roots: tuple[Path, ...] = () if root == home else (root,)
    return FilesystemSandbox(read_root=root, write_roots=write_roots)


@app.command("status")
@with_error_handling("sandbox.status")
def status(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", help="Project root (default: detected)."),
) -> None:
    """Show the sandbox scope for the current context."""
    with services(ctx) as s:
        sandbox = _sandbox_for_path(s, project)
        profile = s.ctx.config.value("permissions.profile") or "workspace"
        data = {
            "profile": profile,
            "read_root": str(sandbox.read_root),
            "write_roots": [str(p) for p in sandbox.write_roots],
            "home_locked": Path.home().resolve() not in set(sandbox.write_roots),
        }
        if is_json(ctx):
            emit_json(success_envelope("sandbox.status", data))
            return
        typer.echo(f"profile: {profile}")
        typer.echo(f"read root:  {sandbox.read_root}")
        typer.echo("write roots:")
        if sandbox.write_roots:
            for p in sandbox.write_roots:
                typer.echo(f"  {p}")
        else:
            typer.echo("  (none — no implicit writable workspace)")


@app.command("profiles")
@with_error_handling("sandbox.profiles")
def profiles(ctx: typer.Context) -> None:
    """List the built-in permission profiles."""
    rows = [{"name": p.value, "label": p.name} for p in PermissionProfile]
    if is_json(ctx):
        emit_json(success_envelope("sandbox.profiles", rows))
        return
    for row in rows:
        typer.echo(f"  {row['name']}")


@app.command("show")
@with_error_handling("sandbox.show")
def show(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    project: str = typer.Option(None, "--project"),
) -> None:
    """Show whether a path is read-writable in the sandbox."""
    with services(ctx) as s:
        sandbox = _sandbox_for_path(s, project)
        target = Path(path).expanduser().resolve()
        readable = _can(sandbox, target, "read")
        writable = _can(sandbox, target, "write")
        data = {"path": str(target), "read": readable, "write": writable}
        if is_json(ctx):
            emit_json(success_envelope("sandbox.show", data))
            return
        typer.echo(
            f"{target}  read={'yes' if readable else 'no'}  write={'yes' if writable else 'no'}"
        )


@app.command("test")
@with_error_handling("sandbox.test")
def test(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    project: str = typer.Option(None, "--project"),
    mode: str = typer.Option("read", "--mode", help="read|write."),
) -> None:
    """Test sandbox access to a path (does not touch the file)."""
    with services(ctx) as s:
        sandbox = _sandbox_for_path(s, project)
        target = Path(path).expanduser().resolve()
        ok = _can(sandbox, target, mode)
        data = {"path": str(target), "mode": mode, "allowed": ok}
        if is_json(ctx):
            emit_json(success_envelope("sandbox.test", data))
            return
        typer.echo(f"{mode} {target}: {'allowed' if ok else 'denied'}")


def _can(sandbox: FilesystemSandbox, target: Path, mode: str) -> bool:
    try:
        if mode == "read":
            sandbox.assert_readable(target)
        else:
            sandbox.assert_writable(target)
        return True
    except Exception:
        return False


@app.command("exec")
@with_error_handling("sandbox.exec")
def exec_cmd(
    ctx: typer.Context,
    command: list[str] = typer.Argument(...),
    project: str = typer.Option(None, "--project"),
    yes: bool = typer.Option(False, "--yes", help="Run without confirmation."),
) -> None:
    """Run a command inside the sandbox working directory."""
    _run_exec(ctx, command, project, yes)


def _run_exec(ctx: typer.Context, command: list[str], project: str | None, yes: bool) -> None:
    with services(ctx) as s:
        sandbox = _sandbox_for_path(s, project)
        limits = ProcessLimits(timeout_s=120, max_output_bytes=256 * 1024)
        workdir = str(sandbox.read_root)
        if not yes:
            typer.echo(f"will run in {workdir}: {' '.join(command)}")
            answer = typer.prompt("Proceed?", default="n").lower()
            if answer not in ("y", "yes"):
                typer.echo("cancelled")
                return
        try:
            proc = subprocess.run(
                command, cwd=workdir, capture_output=True, text=True, timeout=limits.timeout_s
            )
            out = proc.stdout[: limits.max_output_bytes]
            err = proc.stderr[: limits.max_output_bytes]
            code = proc.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", f"timeout after {limits.timeout_s}s", 124
        data = {"command": command, "cwd": workdir, "exit": code, "stdout": out, "stderr": err}
        if is_json(ctx):
            emit_json(success_envelope("sandbox.exec", data))
            return
        if out:
            typer.echo(out.rstrip())
        if err:
            typer.echo(err.rstrip(), err=True)
        typer.echo(f"exit: {code}")


__all__ = ["app"]
