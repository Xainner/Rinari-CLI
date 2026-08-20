"""`rinari permissions` group: effective policy inspection (commands.md 37)."""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.policy.approval_store import ApprovalStore, store_path_for
from rinari.policy.engine import (
    CAPABILITY_FS_READ,
    CAPABILITY_FS_WRITE,
    CAPABILITY_GIT_LOCAL,
    CAPABILITY_NETWORK,
    CAPABILITY_SHELL,
    PolicyEngine,
    SessionScope,
    normalize_profile,
)

from ...shared.errors import InvalidUsageError
from ..output import emit_json, success_envelope

app = typer.Typer(help="Inspect effective permissions.", no_args_is_help=True)

_CAPABILITIES = (
    CAPABILITY_FS_READ,
    CAPABILITY_FS_WRITE,
    CAPABILITY_SHELL,
    CAPABILITY_GIT_LOCAL,
    CAPABILITY_NETWORK,
    "state.read",
    "state.write",
    "mcp.read",
    "mcp.write",
    "browser.read",
    "browser.write",
)


def _scope_for(s, project: str | None) -> SessionScope:
    from rinari.application.session_service import detect_project
    from rinari.projects.git import git_state

    if project:
        root: Path | None = Path(project).resolve()
    else:
        detection = detect_project(Path.cwd(), Path.home())
        root = detection.project_root
    kind = "PROJECT" if (root is not None and git_state(root).available) else "CHAT"
    # Match the runtime's scope derivation (tools.runtime.scope_from_context):
    # PROJECT scopes to the project root; CHAT scopes to the working directory.
    effective_root = root if kind == "PROJECT" else Path.cwd()
    return SessionScope(
        kind=kind,
        root=effective_root,
        cwd=Path.cwd(),
        profile=normalize_profile(s.ctx.config.value("permissions.profile") or "workspace"),
        user_home=Path.home(),
    )


@app.command("show")
@with_error_handling("permissions.show")
def show(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", help="Project root (default: detected)."),
) -> None:
    """Show the effective permission per capability for the current scope."""
    with services(ctx) as s:
        scope = _scope_for(s, project)
        engine = PolicyEngine(network=s.network.policy())
        rows = {cap: engine.decide(cap, scope).action.value for cap in _CAPABILITIES}
        data = {
            "profile": scope.profile.value,
            "kind": scope.kind,
            "root": str(scope.root) if scope.root else None,
            "network": s.network.policy().mode,
            "capabilities": rows,
        }
        if is_json(ctx):
            emit_json(success_envelope("permissions.show", data))
            return
        typer.echo(f"profile {scope.profile.value}  ({scope.kind}, root: {scope.root or '-'})")
        for cap, action in rows.items():
            typer.echo(f"  {cap:<14} {action}")


@app.command("explain")
@with_error_handling("permissions.explain")
def explain(
    ctx: typer.Context,
    capability: str = typer.Argument(...),
    path: str = typer.Option(None, "--path"),
    project: str = typer.Option(None, "--project"),
) -> None:
    """Explain why the policy allows/asks/denies one action."""
    with services(ctx) as s:
        scope = _scope_for(s, project)
        engine = PolicyEngine(network=s.network.policy())
        default_path = str(scope.cwd)
        decision = engine.decide(capability, scope, path=path or default_path)
        data = {
            "capability": capability,
            "action": decision.action.value,
            "reason": decision.reason,
        }
        if is_json(ctx):
            emit_json(success_envelope("permissions.explain", data))
            return
        typer.echo(f"{capability} -> {decision.action.value}")
        typer.echo(f"reason: {decision.reason}")


@app.command("check")
@with_error_handling("permissions.check")
def check(
    ctx: typer.Context,
    capability: str = typer.Argument(...),
    path: str = typer.Option(None, "--path"),
    command: str = typer.Option(None, "--command"),
    project: str = typer.Option(None, "--project"),
) -> None:
    """Check one concrete action (machine-readable)."""
    with services(ctx) as s:
        scope = _scope_for(s, project)
        engine = PolicyEngine(network=s.network.policy())
        default_path = str(scope.cwd)
        decision = engine.decide(capability, scope, path=path or default_path, command=command)
        data = {"capability": capability, "action": decision.action.value}
        if is_json(ctx):
            emit_json(success_envelope("permissions.check", data))
        else:
            typer.echo(decision.action.value)


@app.command("grants")
@with_error_handling("permissions.grants")
def grants(ctx: typer.Context) -> None:
    """List persistent approval grants."""
    with services(ctx) as s:
        store = ApprovalStore(store_path_for(s.ctx.layout))
        rows = [
            {
                "key": key,
                "capability": grant.capability,
                "target": grant.target,
                "scope": grant.scope.value,
                "granted_at": grant.granted_at,
            }
            for key, grant in sorted(store.load().items())
        ]
        if is_json(ctx):
            emit_json(success_envelope("permissions.grants", rows))
            return
        if not rows:
            typer.echo("no persistent grants")
            return
        for row in rows:
            target = f" {row['target']}" if row["target"] else ""
            typer.echo(f"  {row['capability']}{target}  [{row['scope']}]  {row['granted_at']}")


@app.command("revoke")
@with_error_handling("permissions.revoke")
def revoke(
    ctx: typer.Context,
    capability: str = typer.Argument(...),
    target: str = typer.Option(None, "--target"),
) -> None:
    """Revoke persistent grants for a capability (optionally one target)."""
    with services(ctx) as s:
        store = ApprovalStore(store_path_for(s.ctx.layout))
        loaded = store.load()
        removed = []
        for key in list(loaded):
            grant = loaded[key]
            if grant.capability != capability:
                continue
            if target is not None and grant.target != target:
                continue
            removed.append(key)
            del loaded[key]
        if not removed:
            raise InvalidUsageError("No matching grant found")
        store.save(loaded)
        if is_json(ctx):
            emit_json(success_envelope("permissions.revoke", {"removed": removed}))
        else:
            typer.echo(f"revoked {len(removed)} grant(s)")
