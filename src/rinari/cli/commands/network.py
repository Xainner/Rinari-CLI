"""`rinari network`: inspect and manage the network policy (phase 4)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope

app = typer.Typer(help="Inspect and manage the network policy.")


@app.command("status")
@with_error_handling("network.status")
def network_status(ctx: typer.Context) -> None:
    """Show the active network mode, rule counts, and audit size."""
    with services(ctx) as s:
        data = s.network.status()
        if is_json(ctx):
            emit_json(success_envelope("network.status", data))
            return
        typer.echo(f"network.mode: {data['mode']}")
        typer.echo(f"rules: {data['rules']} (allow {data['allow']}, deny {data['deny']})")
        typer.echo(f"events: {data['events']}")


@app.command("test")
@with_error_handling("network.test")
def network_test(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Host or URL to evaluate hypothetically."),
) -> None:
    """Evaluate a host/URL against the current policy without touching it."""
    with services(ctx) as s:
        data = s.network.test(target)
        if is_json(ctx):
            emit_json(success_envelope("network.test", data))
            return
        typer.echo(f"{data['action'].upper()}  {data['target'] or target}")
        typer.echo(f"  reason: {data['reason']}")


@app.command("rules")
@with_error_handling("network.rules")
def network_rules(ctx: typer.Context) -> None:
    """List persistent network rules."""
    with services(ctx) as s:
        rules = s.network.rules()
        if is_json(ctx):
            emit_json(success_envelope("network.rules", {"rules": rules}))
            return
        if not rules:
            typer.echo("No network rules (mode only).")
            return
        for row in rules:
            reason = f"  ({row['reason']})" if row.get("reason") else ""
            typer.echo(f"[{row['decision']}] {row['host']}  {row['scope']}{reason}")


@app.command("allow")
@with_error_handling("network.allow")
def network_allow(
    ctx: typer.Context,
    host: str = typer.Argument(..., help="Host to allow (exact or subdomain)."),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """Persist an allow rule for a host (subdomains covered)."""
    with services(ctx) as s:
        row = s.network.add_rule(host, "allow", reason=reason)
        if is_json(ctx):
            emit_json(success_envelope("network.allow", row))
            return
        typer.echo(f"allowed {row['host']}")


@app.command("deny")
@with_error_handling("network.deny")
def network_deny(
    ctx: typer.Context,
    host: str = typer.Argument(..., help="Host to deny (rules beat the mode)."),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """Persist a deny rule for a host (denies even when network.mode=allow)."""
    with services(ctx) as s:
        row = s.network.add_rule(host, "deny", reason=reason)
        if is_json(ctx):
            emit_json(success_envelope("network.deny", row))
            return
        typer.echo(f"denied {row['host']}")


@app.command("remove")
@with_error_handling("network.remove")
def network_remove(
    ctx: typer.Context,
    host: str = typer.Argument(...),
    decision: str = typer.Option(
        "",
        "--decision",
        help="Limit removal to 'allow' or 'deny' rules (default: both).",
    ),
) -> None:
    """Remove the persisted rule(s) for a host."""
    with services(ctx) as s:
        removed = s.network.remove_rule(host, decision or None)
        if is_json(ctx):
            emit_json(success_envelope("network.remove", {"host": host, "removed": removed}))
            return
        typer.echo(
            f"removed {removed} rule(s) for {host}" if removed else f"no rule found for {host}"
        )


@app.command("history")
@with_error_handling("network.history")
def network_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    session: str = typer.Option(None, "--session"),
) -> None:
    """Show recent network policy decisions (audit trail)."""
    with services(ctx) as s:
        events = s.network.events(limit=limit, session_id=session)
        if is_json(ctx):
            emit_json(success_envelope("network.history", {"events": events}))
            return
        if not events:
            typer.echo("No network events recorded yet.")
            return
        for row in events:
            tool = f" {row['tool']}" if row.get("tool") else ""
            typer.echo(
                f"{row['created_at']}  {row['action'].upper():<6} {row['host']}{tool}  "
                f"{row['reason']}"
            )


__all__ = ["app"]
