"""`rinari mcp` group: MCP server registry + discovery (phase 5).

Per docs/commands.md section 46. Project-scope servers require project trust;
`tools`/`prompts`/`resources` connect (lazily cached) to the server.
"""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError, RinariError

app = typer.Typer(help="Manage MCP servers (stdio).", no_args_is_help=True)


def _project() -> Path | None:
    from rinari.projects.detector import detect_project

    return detect_project(Path.cwd(), Path.home()).project_root


def _row(services_ctx, name: str, scope: str = "global") -> dict | None:
    row = services_ctx.mcp.show(name, scope)
    if row is None and scope == "global":
        row = services_ctx.mcp.show(name, "project")
    return row


@app.command("list")
@with_error_handling("mcp.list")
def mcp_list(ctx: typer.Context) -> None:
    """List configured MCP servers."""
    with services(ctx) as s:
        rows = s.mcp.list()
        if is_json(ctx):
            emit_json(success_envelope("mcp.list", {"servers": rows}))
            return
        if not rows:
            typer.echo("No MCP servers configured (rinari mcp add <name> -- command...).")
            return
        typer.echo(f"{'NAME':<18} {'SCOPE':<8} {'TRANSPORT':<8} {'ENABLED':<8} COMMAND/URL")
        for row in rows:
            target = row.get("command") or row.get("url") or "-"
            typer.echo(
                f"{row['name']:<18} {row['scope']:<8} {row['transport']:<8} "
                f"{'yes' if row['enabled'] else 'no':<8} {target}"
            )


@app.command("add")
@with_error_handling("mcp.add")
def mcp_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Unique server name."),
    scope: str = typer.Option("global", "--scope", help="global | project."),
    env_ref: list[str] = typer.Option(
        None, "--env", help="Env reference KEY=env://PROCESS_VAR (repeatable)."
    ),
    command: list[str] = typer.Argument(None, help="Command to spawn (stdio)."),
) -> None:
    """Register an MCP server (stdio). Secrets only as env:// references."""
    if scope not in ("global", "project"):
        raise InvalidUsageError("--scope must be global or project")
    if not command:
        raise InvalidUsageError("provide the command: rinari mcp add <name> -- cmd [args...]")
    refs: dict[str, str] = {}
    for item in env_ref or []:
        if "=" not in item:
            raise InvalidUsageError(f"--env expected KEY=env://VAR, got {item!r}")
        key, ref = item.split("=", 1)
        refs[key.strip()] = ref.strip()
    project = _project() if scope == "project" else None
    with services(ctx) as s:
        if (
            scope == "project"
            and project is not None
            and s.trust.status(project).state != "trusted"
        ):
            typer.echo(f"warning: {project} is not trusted; the server will stay inert")
        row = s.mcp.add(name, list(command), scope=scope, env_refs=refs)
        if is_json(ctx):
            emit_json(success_envelope("mcp.add", row))
            return
        typer.echo(f"added MCP server {name} ({scope})")


@app.command("remove")
@with_error_handling("mcp.remove")
def mcp_remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """Remove a configured MCP server."""
    with services(ctx) as s:
        removed = s.mcp.remove(name, scope)
        if is_json(ctx):
            emit_json(success_envelope("mcp.remove", {"removed": removed}))
            return
        typer.echo(f"removed {name}" if removed else f"no such server: {name} ({scope})")


@app.command("enable")
@with_error_handling("mcp.enable")
def mcp_enable(
    ctx: typer.Context, name: str, scope: str = typer.Option("global", "--scope")
) -> None:
    """Enable an MCP server."""
    with services(ctx) as s:
        row = s.mcp.enable(name, scope)
        if row is None:
            raise RinariError(f"MCP server not found: {name} ({scope})")
        if is_json(ctx):
            emit_json(success_envelope("mcp.enable", row))
            return
        typer.echo(f"enabled {name}")


@app.command("disable")
@with_error_handling("mcp.disable")
def mcp_disable(
    ctx: typer.Context, name: str, scope: str = typer.Option("global", "--scope")
) -> None:
    """Disable an MCP server (its tools are not offered)."""
    with services(ctx) as s:
        row = s.mcp.disable(name, scope)
        if row is None:
            raise RinariError(f"MCP server not found: {name} ({scope})")
        if is_json(ctx):
            emit_json(success_envelope("mcp.disable", row))
            return
        typer.echo(f"disabled {name}")


@app.command("show")
@with_error_handling("mcp.show")
def mcp_show(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Show an MCP server's record."""
    with services(ctx) as s:
        row = _row(s, name)
        if row is None:
            raise RinariError(f"MCP server not found: {name}")
        if is_json(ctx):
            emit_json(success_envelope("mcp.show", row))
            return
        typer.echo(f"name:      {row['name']}")
        typer.echo(f"scope:     {row['scope']}")
        typer.echo(f"transport: {row['transport']}")
        typer.echo(f"command:   {row.get('command') or '-'}")
        typer.echo(f"url:       {row.get('url') or '-'}")
        typer.echo(f"enabled:   {'yes' if row['enabled'] else 'no'}")


@app.command("connect")
@with_error_handling("mcp.connect")
def mcp_connect(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Connect to the server (initialize handshake) and report serverInfo."""
    with services(ctx) as s:
        data = s.mcp.connect(name, _project())
        if is_json(ctx):
            emit_json(success_envelope("mcp.connect", data))
            return
        info = (data or {}).get("serverInfo") or {}
        typer.echo(f"connected: {name}  server={info.get('name')} v{info.get('version')}")


@app.command("disconnect")
@with_error_handling("mcp.disconnect")
def mcp_disconnect(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Drop the cached connection to the server."""
    with services(ctx) as s:
        dropped = s.mcp.disconnect(name)
        if is_json(ctx):
            emit_json(success_envelope("mcp.disconnect", {"dropped": dropped}))
            return
        typer.echo(f"disconnected {name}" if dropped else f"no active connection to {name}")


@app.command("tools")
@with_error_handling("mcp.tools")
def mcp_tools(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """List the tools the server exposes (connected, normalized)."""
    with services(ctx) as s:
        rows = s.mcp.tools(name, _project())
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "mcp.tools",
                    {
                        "name": name,
                        "tools": [
                            {
                                "name": d.name,
                                "description": d.description,
                                "capabilities": list(d.capabilities),
                                "risk": d.risk,
                            }
                            for d in rows
                        ],
                    },
                )
            )
            return
        for d in rows:
            typer.echo(f"{d.name}  [{','.join(d.capabilities)}] {d.description[:80]}")


@app.command("resources")
@with_error_handling("mcp.resources")
def mcp_resources(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """List resources the server exposes."""
    with services(ctx) as s:
        client = s.mcp.client(name, _project())
        rows = client.list_resources()
        if is_json(ctx):
            emit_json(success_envelope("mcp.resources", {"name": name, "resources": rows}))
            return
        for r in rows:
            typer.echo(f"{r.get('uri')}  {r.get('name') or ''}  {r.get('description') or ''}")


@app.command("prompts")
@with_error_handling("mcp.prompts")
def mcp_prompts(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """List prompts the server exposes."""
    with services(ctx) as s:
        client = s.mcp.client(name, _project())
        rows = client.list_prompts()
        if is_json(ctx):
            emit_json(success_envelope("mcp.prompts", {"name": name, "prompts": rows}))
            return
        for p in rows:
            typer.echo(f"{p.get('name')}  {p.get('description') or ''}")


@app.command("test")
@with_error_handling("mcp.test")
def mcp_test(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Connect and count tools (health check)."""
    with services(ctx) as s:
        data = s.mcp.test(name, _project())
        if is_json(ctx):
            emit_json(success_envelope("mcp.test", data))
            return
        if data.get("ok"):
            typer.echo(f"OK: {data['tools']} tool(s) available")
        else:
            typer.echo(f"FAIL [{data.get('error')}]: {data.get('message')}")


@app.command("logs")
@with_error_handling("mcp.logs")
def mcp_logs(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Filter by server name."),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """Show recent MCP connection/operation log entries (process-local)."""
    with services(ctx) as s:
        entries = s.mcp.logs(name, limit)
        if is_json(ctx):
            emit_json(success_envelope("mcp.logs", {"entries": entries}))
            return
        if not entries:
            typer.echo("No MCP log entries yet (this process).")
            return
        for e in reversed(entries):
            typer.echo(f"{e['at']}  {e['level']:<6} {e['server']:<16} {e['message']}")


__all__ = ["app"]
