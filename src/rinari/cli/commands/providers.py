"""`rinari providers` - manage the saved provider registry (docs/commands.md 14)."""

from __future__ import annotations

import typer

from rinari.application.provider_service import AddProviderInput
from rinari.cli.deps import fail, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import provider_dict
from rinari.providers.registry import PROVIDER_TYPES
from rinari.shared.errors import InvalidUsageError, ProviderModelError

app = typer.Typer(help="Manage saved providers.", no_args_is_help=True)

STATUS_LABELS = {True: "connected", False: "disconnected", None: "-"}


@app.command("list")
@with_error_handling("providers.list")
def providers_list(
    ctx: typer.Context,
    connected: bool = typer.Option(False, "--connected", help="Only providers marked connected."),
    type_filter: str = typer.Option(
        None, "--type", help="Filter by provider type (openai/anthropic/custom)."
    ),
) -> None:
    """List saved providers."""
    with services(ctx) as s:
        records = s.providers.list()
        current = s.providers.current()
        active_id = current.provider.id if current is not None else None
        if type_filter:
            records = [r for r in records if r.type == type_filter]
        if connected:
            records = [r for r in records if r.status_connected is True]
        if is_json(ctx):
            data = [
                provider_dict(
                    r, active=(r.id == active_id), credential_ref=s.providers.credential_ref(r)
                )
                for r in records
            ]
            emit_json(success_envelope("providers.list", data))
            return
        if not records:
            typer.echo(
                "No providers saved. Add one with: rinari providers add <openai|anthropic|custom>"
            )
            return
        typer.echo(f"{'ALIAS':<20} {'TYPE':<10} {'AUTH':<8} {'STATUS':<12} ACTIVE")
        for r in records:
            typer.echo(
                f"{r.alias:<20} {r.type:<10} {r.auth_method:<8} "
                f"{STATUS_LABELS[r.status_connected]:<12} {'*' if r.id == active_id else ''}"
            )


@app.command("add")
@with_error_handling("providers.add")
def providers_add(
    ctx: typer.Context,
    provider_type: str = typer.Argument(None, help="Provider type: openai, anthropic, or custom."),
    name: str = typer.Option(None, "--name", "-n", help="Provider alias."),
    endpoint: str = typer.Option(
        None, "--endpoint", help="Provider base URL (required for custom)."
    ),
    account: str = typer.Option(None, "--account", help="Account hint (work, personal, ...)."),
    api_key: str = typer.Option(None, "--api-key", help="API key value (stored as a secret)."),
    api_key_env: str = typer.Option(
        None, "--api-key-env", help="Environment variable holding the API key."
    ),
    no_auth: bool = typer.Option(False, "--no-auth", help="Provider requires no credential."),
    protocol: str = typer.Option(
        None, "--protocol", help="Wire protocol for custom providers (openai-compatible)."
    ),
) -> None:
    """Save a new provider entry. Never removes or replaces another one."""
    if provider_type is None:
        raise InvalidUsageError(
            "provider type is required", hint="rinari providers add <openai|anthropic|custom>"
        )
    if provider_type not in PROVIDER_TYPES:
        raise InvalidUsageError(
            f"unknown provider type {provider_type!r}",
            hint=f"Expected one of: {', '.join(sorted(PROVIDER_TYPES))}",
        )
    with services(ctx) as s:
        record = s.providers.add(
            AddProviderInput(
                alias=name or provider_type,
                provider_type=provider_type,
                auth_method="none" if no_auth else "api-key",
                endpoint=endpoint,
                account_hint=account,
                secret=api_key,
                secret_env=api_key_env,
                settings={"protocol": protocol} if protocol else {},
            )
        )
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "providers.add",
                    provider_dict(record, credential_ref=s.providers.credential_ref(record)),
                )
            )
            return
        typer.echo(f"Provider {record.alias!r} saved (type {record.type}).")


@app.command("login")
@with_error_handling("providers.login")
def providers_login(ctx: typer.Context, alias: str = typer.Argument(...)) -> None:
    """Establish login-based auth when the adapter supports it."""
    with services(ctx) as s:
        record = s.providers.login(alias)
        if is_json(ctx):
            emit_json(success_envelope("providers.login", provider_dict(record)))
            return
        typer.echo(f"Login complete for {record.alias!r}.")


@app.command("logout")
@with_error_handling("providers.logout")
def providers_logout(ctx: typer.Context, alias: str = typer.Argument(...)) -> None:
    """End authentication; the provider, models, and settings remain saved."""
    with services(ctx) as s:
        record = s.providers.logout(alias)
        if is_json(ctx):
            emit_json(success_envelope("providers.logout", provider_dict(record)))
            return
        typer.echo(f"Logged out {record.alias!r}. Configuration preserved.")


@app.command("auth")
@with_error_handling("providers.auth")
def providers_auth(
    ctx: typer.Context,
    alias: str = typer.Argument(...),
    api_key: str = typer.Option(None, "--api-key", help="API key value (stored as a secret)."),
    api_key_env: str = typer.Option(
        None, "--api-key-env", help="Environment variable holding the API key."
    ),
    do_login: bool = typer.Option(
        False, "--login", help="Use interactive login (adapter must support it)."
    ),
) -> None:
    """Configure the authentication method of a saved provider."""
    with services(ctx) as s:
        if do_login:
            record = s.providers.login(alias)
        else:
            record = s.providers.set_auth(alias, secret=api_key, secret_env=api_key_env)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "providers.auth",
                    provider_dict(record, credential_ref=s.providers.credential_ref(record)),
                )
            )
            return
        typer.echo(f"Authentication updated for {record.alias!r} ({record.auth_method}).")


@app.command("show")
@with_error_handling("providers.show")
def providers_show(ctx: typer.Context, alias: str = typer.Argument(...)) -> None:
    """Show provider details. Secrets are always redacted."""
    with services(ctx) as s:
        record = s.providers.get(alias)
        current = s.providers.current()
        data = provider_dict(
            record,
            active=current is not None and current.provider.id == record.id,
            credential_ref=s.providers.credential_ref(record),
        )
        data["default_model"] = s.providers.model_alias(record.default_model_id)
        if is_json(ctx):
            emit_json(success_envelope("providers.show", data))
            return
        typer.echo(f"id          {record.id}")
        typer.echo(f"alias       {record.alias}")
        typer.echo(f"type        {record.type}")
        typer.echo(f"auth        {record.auth_method}")
        status = STATUS_LABELS[record.status_connected]
        typer.echo(
            f"status      {status}"
            + (f" (checked {record.status_checked_at})" if record.status_checked_at else "")
        )
        typer.echo(f"endpoint    {record.endpoint or '-'}")
        typer.echo(f"account     {record.account_hint or '-'}")
        typer.echo(f"default     {data['default_model'] or '-'}")
        typer.echo(f"created     {record.created_at}")
        typer.echo(f"updated     {record.updated_at}")


@app.command("test")
@with_error_handling("providers.test")
def providers_test(ctx: typer.Context, alias: str = typer.Argument(...)) -> None:
    """Check reachability, auth, and model discovery for a provider."""
    with services(ctx) as s:
        health = s.providers.test(alias)
        data = {
            "alias": alias,
            "connected": health.connected,
            "detail": health.detail,
            "models_discovered": len(health.models),
        }
        if not health.connected:
            err = ProviderModelError(health.detail or "provider not reachable")
            if is_json(ctx):
                fail(ctx, "providers.test", err)
            typer.echo(f"FAIL {alias}: {health.detail or 'not reachable'}", err=True)
            raise typer.Exit(11)
        if is_json(ctx):
            emit_json(success_envelope("providers.test", data))
            return
        typer.echo(f"OK {alias} connected ({len(health.models)} models discovered)")


@app.command("rename")
@with_error_handling("providers.rename")
def providers_rename(
    ctx: typer.Context,
    alias: str = typer.Argument(...),
    new_alias: str = typer.Argument(...),
) -> None:
    """Rename a provider alias. Internal IDs and references are preserved."""
    with services(ctx) as s:
        record = s.providers.rename(alias, new_alias)
        if is_json(ctx):
            emit_json(success_envelope("providers.rename", provider_dict(record)))
            return
        typer.echo(f"Renamed {alias!r} -> {new_alias!r} (id {record.id} unchanged).")


@app.command("discover")
@with_error_handling("providers.discover")
def providers_discover(ctx: typer.Context) -> None:
    """Detect local endpoints and environment credential references."""
    with services(ctx) as s:
        candidates = s.providers.discover()
        data = [
            {
                "source": c.source,
                "name": c.name,
                "detail": c.detail,
                "provider_type": c.provider_type,
                "endpoint": c.endpoint,
            }
            for c in candidates
        ]
        if is_json(ctx):
            emit_json(success_envelope("providers.discover", data))
            return
        if not candidates:
            typer.echo("No candidates found (no local endpoints, no credential env vars).")
            return
        for c in candidates:
            typer.echo(
                f"{c.name}  [{c.source}]  {c.detail}  -> {c.provider_type}"
                + (f" @ {c.endpoint}" if c.endpoint else "")
            )


@app.command("remove")
@with_error_handling("providers.remove")
def providers_remove(
    ctx: typer.Context,
    alias: str = typer.Argument(...),
    switch_to: str = typer.Option(
        None, "--switch-to", help="Provider to activate if this one is active."
    ),
    keep_credentials: bool = typer.Option(
        False, "--keep-credentials", help="Keep the stored credential."
    ),
) -> None:
    """Remove one provider entry (explicit destructive command)."""
    with services(ctx) as s:
        record = s.providers.remove(alias, switch_to=switch_to, keep_credentials=keep_credentials)
        data = {
            "removed": provider_dict(record),
            "switch_to": switch_to,
            "kept_credentials": keep_credentials,
        }
        if is_json(ctx):
            emit_json(success_envelope("providers.remove", data))
            return
        typer.echo(f"Removed provider {record.alias!r} (id {record.id}).")
