"""`rinari tools` group: inspect and test tools (commands.md 42)."""

from __future__ import annotations

import json

import typer

from rinari.cli.deps import app_context, is_json, with_error_handling
from rinari.shared.errors import NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Inspect and test the tool registry.", no_args_is_help=True)


def _registry():
    from rinari.tools.catalog import builtin_catalog

    return builtin_catalog()


def _tool_row(tool) -> dict:
    from rinari.tools.availability import availability

    return {
        "name": tool.name,
        "description": tool.description,
        "capabilities": list(tool.capabilities) or None,
        "permissions": list(tool.permissions) or None,
        "risk": tool.risk,
        "side_effects": tool.side_effects,
        "namespace": tool.namespace,
        "always_loaded": tool.always_loaded,
        "availability": availability(tool.name),
    }


@app.command("list")
@with_error_handling("tools.list")
def list_cmd(
    ctx: typer.Context,
    full: bool = typer.Option(False, "--full", help="Include description and capabilities."),
) -> None:
    """List all registered tools."""
    registry = _registry()
    names = registry.names()
    rows = [_tool_row(registry.get(n)) for n in names]
    if is_json(ctx):
        emit_json(success_envelope("tools.list", rows if full else [r["name"] for r in rows]))
        return
    if full:
        for row in rows:
            caps = ", ".join(row["capabilities"] or ()) or "-"
            typer.echo(f"{row['name']:<28} {row['risk']:<8} {caps}")
            typer.echo(f"    {row['description']}")
    else:
        for row in rows:
            typer.echo(f"{row['name']:<28} {row['risk']}")


@app.command("search")
@with_error_handling("tools.search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
) -> None:
    """Search tools by name, description, or capability."""
    registry = _registry()
    rows = [_tool_row(t) for t in registry.search(query)]
    if is_json(ctx):
        emit_json(success_envelope("tools.search", rows))
        return
    if not rows:
        typer.echo(f"no tools matching {query!r}")
        return
    for row in rows:
        typer.echo(f"{row['name']:<28} {row['risk']:<8} {row['description']}")


@app.command("show")
@with_error_handling("tools.show")
def show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Show a tool's definition, schema, and capabilities."""
    registry = _registry()
    tool = registry.get(name)
    if tool is None:
        raise NotFoundError(f"Tool not found: {name}")
    data = {
        **_tool_row(tool),
        "idempotent": tool.idempotent,
        "timeout_ms": tool.timeout_ms,
        "max_output_bytes": tool.max_output_bytes,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
    }
    if is_json(ctx):
        emit_json(success_envelope("tools.show", data))
        return
    typer.echo(f"{tool.name}  risk={tool.risk}  namespace={tool.namespace or '-'}")
    typer.echo(f"  {tool.description}")
    typer.echo(f"  capabilities: {', '.join(tool.capabilities) or '-'}")
    typer.echo(f"  side effects: {tool.side_effects or '-'}")
    typer.echo(f"  idempotent: {tool.idempotent}  timeout: {tool.timeout_ms}ms")
    typer.echo(f"  input schema: {json.dumps(tool.input_schema, sort_keys=True)}")


@app.command("permissions")
@with_error_handling("tools.permissions")
def permissions(
    ctx: typer.Context,
    name: str = typer.Argument(...),
) -> None:
    """Show the policy decision for a tool's capabilities (default profile)."""
    registry = _registry()
    tool = registry.get(name)
    if tool is None:
        raise NotFoundError(f"Tool not found: {name}")
    from pathlib import Path

    from rinari.policy.engine import (
        PolicyEngine,
        SessionScope,
        normalize_profile,
    )

    with app_context(ctx) as c:
        profile_name = c.config.value("permissions.profile") or "workspace"
    engine = PolicyEngine()
    scope = SessionScope(
        kind="PROJECT",
        root=Path.cwd(),
        cwd=Path.cwd(),
        profile=normalize_profile(profile_name),
        user_home=Path.home(),
    )
    rows = [
        {"capability": cap, "action": engine.decide(cap, scope).action.value}
        for cap in tool.capabilities
    ]
    if is_json(ctx):
        emit_json(
            success_envelope(
                "tools.permissions", {"tool": name, "profile": profile_name, "decisions": rows}
            )
        )
        return
    typer.echo(f"{name}  (profile {profile_name})")
    for row in rows:
        typer.echo(f"  {row['capability']:<16} {row['action']}")


@app.command("doctor")
@with_error_handling("tools.doctor")
def doctor(ctx: typer.Context) -> None:
    """Validate every tool's schema and handler wiring."""
    registry = _registry()
    problems = []
    for name in registry.names():
        tool = registry.get(name)
        if not tool.name:
            problems.append(f"{name}: empty name")
        if not isinstance(tool.input_schema, dict):
            problems.append(f"{name}: input_schema is not an object")
        elif "type" not in tool.input_schema:
            problems.append(f"{name}: input_schema missing 'type'")
        if not callable(tool.handler):
            problems.append(f"{name}: handler not callable")
    if is_json(ctx):
        emit_json(
            success_envelope("tools.doctor", {"tools": len(registry.names()), "problems": problems})
        )
        return
    if problems:
        for p in problems:
            typer.echo(f"  [x] {p}", err=True)
        typer.echo(f"{len(problems)} problem(s) in {len(registry.names())} tools", err=True)
        raise typer.Exit(1)
    typer.echo(f"OK: {len(registry.names())} tools, schemas valid")
