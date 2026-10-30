"""`rinari mcp` group: MCP server registry + discovery (phase 5).

Per docs/commands.md section 46. Project-scope servers require project trust;
`tools`/`prompts`/`resources` connect (lazily cached) to the server.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import (
    AuthenticationRequiredError,
    CancelledError,
    InvalidUsageError,
    RinariError,
)

app = typer.Typer(help="Manage MCP servers (stdio and remote HTTP).", no_args_is_help=True)


def _project() -> Path | None:
    from rinari.projects.detector import detect_project

    return detect_project(Path.cwd(), Path.home()).project_root


def _safe_row(services_ctx, row: dict) -> dict:
    """Row for --json output: the config as a view (flags, never secrets)."""
    data = {k: v for k, v in row.items() if k != "config_json"}
    data.update(services_ctx.mcp.view(row))
    return data


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
            emit_json(success_envelope("mcp.list", {"servers": [_safe_row(s, r) for r in rows]}))
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
    url: str = typer.Option(
        None, "--url", help="Remote server URL (Streamable HTTP) instead of a command."
    ),
    bearer_env: str = typer.Option(
        None, "--bearer-env", help="Bearer token from this environment variable (http)."
    ),
    bearer: bool = typer.Option(
        False, "--bearer", help="Ask for a bearer token (hidden; stored securely)."
    ),
    header: list[str] = typer.Option(
        None, "--header", help="Plain header 'Name: value' (repeatable, http)."
    ),
    secret_header: list[str] = typer.Option(
        None,
        "--secret-header",
        help="Secret header: 'Name' (asked hidden) or 'Name=env://VAR' (repeatable).",
    ),
    timeout: float = typer.Option(None, "--timeout", help="Request timeout in seconds."),
    use_oauth: bool = typer.Option(
        False, "--oauth", help="Sign in with OAuth (then: rinari mcp login <name>)."
    ),
    client_id: str = typer.Option(
        None, "--client-id", help="Your own OAuth client ID (servers without registration)."
    ),
    client_secret: bool = typer.Option(
        False, "--client-secret", help="Ask for your OAuth client secret (hidden)."
    ),
    client_secret_env: str = typer.Option(
        None, "--client-secret-env", help="OAuth client secret from this environment variable."
    ),
    oauth_scope: list[str] = typer.Option(
        None, "--oauth-scope", help="OAuth scope to ask for (repeatable)."
    ),
    redirect_port: int = typer.Option(
        None, "--redirect-port", help="Fixed loopback port for the OAuth redirect."
    ),
    client_metadata_url: str = typer.Option(
        None, "--client-metadata-url", help="OAuth client ID metadata document URL (CIMD)."
    ),
    command: list[str] = typer.Argument(None, help="Command to spawn (stdio)."),
) -> None:
    """Register an MCP server: a command (stdio) or a --url (remote).

    Secrets are never written to the database: tokens and secret headers go
    to the OS credential store (or stay as env:// references).
    """
    oauth_options = (
        client_id,
        client_secret,
        client_secret_env,
        oauth_scope,
        redirect_port,
        client_metadata_url,
    )
    if any(oauth_options) and not use_oauth:
        raise InvalidUsageError("--client-id, --oauth-scope and the like need --oauth")
    if scope not in ("global", "project"):
        raise InvalidUsageError("--scope must be global or project")
    if not command and not url:
        raise InvalidUsageError(
            "provide the command (rinari mcp add <name> -- cmd [args...]) or --url"
        )
    if command and url:
        raise InvalidUsageError("use either a command or --url, not both")
    refs: dict[str, str] = {}
    for item in env_ref or []:
        if "=" not in item:
            raise InvalidUsageError("--env expected KEY=env://VAR")
        key, ref = item.split("=", 1)
        refs[key.strip()] = ref.strip()
    auth: dict | None = None
    headers: dict[str, dict] = {}
    if url:
        if bearer_env and bearer:
            raise InvalidUsageError("use either --bearer or --bearer-env")
        if bearer_env:
            auth = {"kind": "bearer", "token": f"env://{bearer_env.strip()}"}
        elif bearer:
            auth = {"kind": "bearer", "token": typer.prompt("Bearer token", hide_input=True)}
        for item in header or []:
            if ":" not in item:
                raise InvalidUsageError("--header expected 'Name: value'")
            key, value = item.split(":", 1)
            headers[key.strip()] = {"value": value.strip(), "secret": False}
        for item in secret_header or []:
            if "=" in item:
                key, ref = item.split("=", 1)
                headers[key.strip()] = {"value": ref.strip(), "secret": True}
            else:
                key = item.strip()
                value = typer.prompt(f"Value for header {key}", hide_input=True)
                headers[key] = {"value": value, "secret": True}
        if auth is None and headers and any(h["secret"] for h in headers.values()):
            auth = {"kind": "headers"}
        if use_oauth:
            if auth is not None and auth["kind"] == "bearer":
                raise InvalidUsageError("use either --oauth or --bearer")
            if client_secret and client_secret_env:
                raise InvalidUsageError("use either --client-secret or --client-secret-env")
            auth = {"kind": "oauth"}
            if client_id:
                auth["client_id"] = client_id
            if client_secret:
                auth["client_secret"] = typer.prompt("OAuth client secret", hide_input=True)
            elif client_secret_env:
                auth["client_secret"] = f"env://{client_secret_env.strip()}"
            if oauth_scope:
                auth["scopes"] = list(oauth_scope)
            if redirect_port:
                auth["redirect_port"] = redirect_port
            if client_metadata_url:
                auth["client_metadata_url"] = client_metadata_url
    elif bearer or bearer_env or header or secret_header or use_oauth:
        raise InvalidUsageError("--bearer/--header/--secret-header/--oauth apply to --url servers")
    project = _project() if scope == "project" else None
    with services(ctx) as s:
        if (
            scope == "project"
            and project is not None
            and s.trust.status(project).state != "trusted"
        ):
            typer.echo(f"warning: {project} is not trusted; the server will stay inert")
        try:
            row = s.mcp.add(
                name,
                list(command or []),
                scope=scope,
                env_refs=refs,
                url=url,
                auth=auth,
                headers=headers or None,
                timeout_s=timeout,
            )
        except ValueError as exc:
            raise InvalidUsageError(str(exc)) from exc
        if is_json(ctx):
            emit_json(success_envelope("mcp.add", _safe_row(s, row)))
            return
        typer.echo(f"added MCP server {name} ({scope}, {row['transport']})")


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
            emit_json(success_envelope("mcp.show", _safe_row(s, row)))
            return
        view = s.mcp.view(row)
        typer.echo(f"name:      {row['name']}")
        typer.echo(f"scope:     {row['scope']}")
        typer.echo(f"transport: {row['transport']}")
        typer.echo(f"command:   {row.get('command') or '-'}")
        typer.echo(f"url:       {row.get('url') or '-'}")
        typer.echo(f"enabled:   {'yes' if row['enabled'] else 'no'}")
        if row["transport"] == "http":
            token = view["auth"].get("token") or {}
            state = "configured" if token.get("configured") else "missing"
            suffix = f" ({state})" if view["auth"]["kind"] == "bearer" else ""
            typer.echo(f"auth:      {view['auth']['kind']}{suffix}")
            if view["auth"]["kind"] == "oauth":
                _show_oauth(s, row, view["auth"])
            for h in view["headers"]:
                shown = "<secret>" if h["secret"] else h.get("value")
                typer.echo(f"header:    {h['name']}: {shown}")
        for e in view["env"]:
            source = f"env://{e['env_var']}" if e.get("env_var") else "<secret>"
            typer.echo(f"env:       {e['name']}={source}")
        for warning in view["warnings"]:
            typer.echo(f"warning:   {warning}")


def _show_oauth(s, row: dict, auth: dict) -> None:
    from rinari.mcp.oauth import OAuthManager

    if auth.get("client_id"):
        secret = "configured" if auth["client_secret"]["configured"] else "none"
        typer.echo(f"client:    {auth['client_id']} (secret: {secret})")
    if auth.get("scopes"):
        typer.echo(f"scopes:    {' '.join(auth['scopes'])}")
    if auth.get("redirect_port"):
        typer.echo(f"redirect:  http://127.0.0.1:{auth['redirect_port']}/callback")
    status = OAuthManager(s.credentials, home=s.ctx.home).status(row["id"])
    if not status["signed_in"]:
        typer.echo(f"sign-in:   no (rinari mcp login {row['name']})")
    elif status["reauth_required"]:
        typer.echo(f"sign-in:   expired (rinari mcp login {row['name']})")
    else:
        expiry = f", expires {status['expires_at']}" if status["expires_at"] else ""
        refresh = ", renews itself" if status["has_refresh_token"] else ""
        typer.echo(f"sign-in:   yes{expiry}{refresh}")


def _oauth_row(s, name: str) -> tuple[dict, Any, str | None]:
    """Row, OAuth config and client secret of a saved oauth server."""
    from rinari.mcp.config import AUTH_OAUTH, TRANSPORT_HTTP, OAuthConfig, draft_from_row

    row = _row(s, name)
    if row is None:
        raise RinariError(f"MCP server not found: {name}")
    draft = draft_from_row(row)
    if draft.transport != TRANSPORT_HTTP or draft.auth_kind != AUTH_OAUTH:
        raise InvalidUsageError(
            f"{name} does not use OAuth (rinari mcp add {name} --url <url> --oauth)"
        )
    oauth = draft.oauth or OAuthConfig()
    secret = None
    if oauth.client_secret is not None and oauth.client_secret.ref:
        secret = s.credentials.resolve(oauth.client_secret.ref)
    return row, oauth, secret


@app.command("login")
@with_error_handling("mcp.login")
def mcp_login(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening the browser."
    ),
    timeout: float = typer.Option(300.0, "--timeout", help="Seconds to wait for the browser."),
) -> None:
    """Sign in to a remote MCP server with OAuth in your browser."""
    from rinari.mcp.oauth import OAuthError, OAuthManager

    with services(ctx) as s:
        row, oauth, secret = _oauth_row(s, name)
        manager = OAuthManager(s.credentials, home=s.ctx.home)
        try:
            try:
                flow = manager.start(
                    row["id"],
                    row["name"],
                    row["url"],
                    oauth,
                    client_secret=secret,
                    open_browser=not no_browser,
                    timeout_s=timeout,
                )
            except OAuthError as exc:
                raise AuthenticationRequiredError(exc.message, hint=exc.hint) from exc
            # The URL goes to stderr so --json output stays one document.
            typer.echo(f"Sign in to {name} in your browser:", err=True)
            typer.echo(flow["authorize_url"], err=True)
            typer.echo("Waiting for the browser…", err=True)
            result = None
            deadline = time.monotonic() + timeout + 5
            try:
                # Short waits keep Ctrl+C responsive (also on Windows).
                while result is None and time.monotonic() < deadline:
                    result = manager.wait(flow["flow_id"], 0.5)
            except KeyboardInterrupt:
                manager.cancel(row["id"])
                raise CancelledError("sign-in cancelled") from None
        finally:
            manager.close()
        if result is None or result["outcome"] != "signed_in":
            error = (result or {}).get("error") or {}
            raise AuthenticationRequiredError(
                error.get("message") or "the sign-in did not finish", hint=error.get("hint")
            )
        s.mcp.disconnect(name)  # the next use connects with the token
        if is_json(ctx):
            emit_json(success_envelope("mcp.login", {"name": name, **result}))
            return
        typer.echo(f"signed in to {name}")


@app.command("logout")
@with_error_handling("mcp.logout")
def mcp_logout(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Sign out of an OAuth MCP server: revoke the tokens (if it can) and delete them."""
    from rinari.mcp.oauth import OAuthManager

    with services(ctx) as s:
        row, oauth, secret = _oauth_row(s, name)
        result = OAuthManager(s.credentials, home=s.ctx.home).signout(
            row["id"], oauth, client_secret=secret
        )
        s.mcp.disconnect(name)
        if is_json(ctx):
            emit_json(success_envelope("mcp.logout", {"name": name, **result}))
            return
        if not result["removed"]:
            typer.echo(f"{name} was not signed in")
            return
        revoked = "revoked and deleted" if result["revoked"] else "deleted"
        typer.echo(f"signed out of {name} (tokens {revoked})")


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
            typer.echo(f"OK: {data['tools']} tool(s) available ({data.get('latency_ms')} ms)")
        else:
            status = f" HTTP {data['http_status']}" if data.get("http_status") else ""
            typer.echo(f"FAIL [{data.get('code')}{status}]: {data.get('message')}")
            if data.get("hint"):
                typer.echo(f"hint: {data['hint']}")


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
