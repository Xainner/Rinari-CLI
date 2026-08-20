"""`rinari project` group: inspect project state and instructions (commands.md 28)."""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.instructions.resolver import provenance_for, resolve_project_instructions
from rinari.projects.git import git_state

from ..output import emit_json, success_envelope

app = typer.Typer(help="Inspect project state and instructions.", no_args_is_help=True)


def _detect(cwd: Path, home: Path):
    from rinari.application.session_service import detect_project

    return detect_project(cwd, home)


@app.command("current")
@with_error_handling("project.current")
def current(ctx: typer.Context) -> None:
    """Show the detected project root for the current directory."""
    with services(ctx) as _:
        det = _detect(Path.cwd(), Path.home())
        data = {
            "root": str(det.project_root) if det.project_root else None,
            "marker": det.marker,
            "secondary_markers": list(det.secondary_markers),
        }
        if is_json(ctx):
            emit_json(success_envelope("project.current", data))
            return
        if det.project_root:
            typer.echo(f"root:   {det.project_root}")
            typer.echo(f"marker: {det.marker or '-'}")
        else:
            typer.echo("no project detected in the current directory")


@app.command("root")
@with_error_handling("project.root")
def root_cmd(ctx: typer.Context) -> None:
    """Print only the detected project root."""
    with services(ctx) as _:
        det = _detect(Path.cwd(), Path.home())
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "project.root", {"root": str(det.project_root) if det.project_root else None}
                )
            )
            return
        if det.project_root:
            typer.echo(str(det.project_root))


@app.command("info")
@with_error_handling("project.info")
def info(ctx: typer.Context) -> None:
    """Show project identity, git state, and trust."""
    cwd = Path.cwd()
    home = Path.home()
    with services(ctx) as s:
        det = _detect(cwd, home)
        root = det.project_root
        git = git_state(root) if root else None
        trust = s.trust.status(root) if root else None
        data = {
            "root": str(root) if root else None,
            "marker": det.marker,
            "git": {
                "available": git.available,
                "branch": git.branch,
                "head": git.head,
                "dirty": git.dirty,
            }
            if git
            else None,
            "trust": {"state": trust.state, "fingerprint": trust.fingerprint} if trust else None,
        }
        if is_json(ctx):
            emit_json(success_envelope("project.info", data))
            return
        typer.echo(f"root:   {root or '-'}")
        typer.echo(f"marker: {det.marker or '-'}")
        if git:
            dirty = " (dirty)" if git.dirty else ""
            typer.echo(f"git:    {git.branch or 'detached'} @ {(git.head or '-')[:8]}{dirty}")
        if trust:
            typer.echo(f"trust:  {trust.state}")


@app.command("list")
@with_error_handling("project.list")
def list_cmd(ctx: typer.Context) -> None:
    """List known projects in the registry."""
    with services(ctx) as s:
        rows = [
            {"id": r.id, "root": r.canonical_root, "fingerprint": r.git_fingerprint}
            for r in s.ctx.project_repo.list()
        ]
        if is_json(ctx):
            emit_json(success_envelope("project.list", rows))
            return
        if not rows:
            typer.echo("no registered projects")
            return
        for row in rows:
            typer.echo(f"  {row['id']}  {row['root']}")


@app.command("add")
@with_error_handling("project.add")
def add(ctx: typer.Context, path: str = typer.Argument(".")) -> None:
    """Register a project root in the registry."""
    with services(ctx) as s:
        record = s.projects.upsert(Path(path).resolve())
        data = {"id": record.id, "root": record.canonical_root}
        if is_json(ctx):
            emit_json(success_envelope("project.add", data))
            return
        typer.echo(f"added {record.canonical_root} ({record.id})")


@app.command("remove")
@with_error_handling("project.remove")
def remove(ctx: typer.Context, path: str = typer.Argument(".")) -> None:
    """Remove a project from the registry."""
    with services(ctx) as s:
        target = Path(path).resolve()
        record = s.ctx.project_repo.get_by_root(str(target))
        if record is None:
            from rinari.shared.errors import NotFoundError

            raise NotFoundError(f"Project not registered: {target}")
        s.ctx.db.execute("DELETE FROM projects WHERE id = ?", (record.id,))
        if is_json(ctx):
            emit_json(success_envelope("project.remove", {"id": record.id}))
            return
        typer.echo(f"removed {record.canonical_root}")


@app.command("instructions")
@with_error_handling("project.instructions")
def instructions(
    ctx: typer.Context,
    show: bool = typer.Option(False, "--show", help="Print the resolved instruction content."),
    explain: bool = typer.Option(False, "--explain", help="Show per-file provenance."),
) -> None:
    """Show the layered project instruction chain for the current directory."""
    cwd = Path.cwd()
    home = Path.home()
    with services(ctx) as s:
        det = _detect(cwd, home)
        trusted = True
        if det.project_root:
            trusted = s.trust.is_trusted(det.project_root)
        files = resolve_project_instructions(det.project_root, cwd, trusted=trusted)
        rows = [
            {
                "path": str(f.path),
                "relative": f.relative,
                "kind": f.kind,
                "scope": f.scope,
                "trust": f.trust,
                "sha256": f.sha256,
                "size_bytes": f.size_bytes,
            }
            for f in files
        ]
        if is_json(ctx):
            emit_json(success_envelope("project.instructions", rows))
            return
        if not files:
            typer.echo("no instruction files found")
            return
        for f in files:
            typer.echo(f"[{f.scope}] {f.path}  ({f.kind}, trust={f.trust}, {f.size_bytes}B)")
            if explain:
                typer.echo(f"    provenance: {provenance_for(f)}")
        if show:
            typer.echo()
            for f in files:
                typer.echo(f"--- {f.path} ---")
                typer.echo(f.content.rstrip())


@app.command("doctor")
@with_error_handling("project.doctor")
def doctor(ctx: typer.Context) -> None:
    """Check project health: detection, git, trust, instructions."""
    cwd = Path.cwd()
    home = Path.home()
    with services(ctx) as s:
        det = _detect(cwd, home)
        root = det.project_root
        checks = {}
        checks["detected"] = root is not None
        git = git_state(root) if root else None
        checks["git"] = bool(git and git.available) if root else False
        trust = s.trust.status(root) if root else None
        checks["trusted"] = bool(trust and trust.state == "trusted") if root else False
        files = (
            resolve_project_instructions(root, cwd, trusted=checks.get("trusted", False))
            if root
            else []
        )
        checks["instructions"] = len(files)
        data = {"root": str(root) if root else None, "checks": checks}
        if is_json(ctx):
            emit_json(success_envelope("project.doctor", data))
            return
        if not root:
            typer.echo("no project detected")
            return
        typer.echo(f"project {root}")
        for key, value in checks.items():
            mark = "ok" if value else ("present" if key == "instructions" else "missing")
            suffix = f" ({value} files)" if key == "instructions" else ""
            typer.echo(f"  {key:<14} {mark}{suffix}")
