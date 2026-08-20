"""`rinari secrets` group: secret references (never plaintext values).

Commands.md 41. Values are stored through the OS keychain / credential store;
this surface only exposes references, existence, and health.
"""

from __future__ import annotations

import os

import typer

from rinari.application.credentials import SecretRef
from rinari.cli.deps import is_json, services, with_error_handling
from rinari.shared.errors import InvalidUsageError, NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Manage secret references (values never shown).", no_args_is_help=True)


@app.command("list")
@with_error_handling("secrets.list")
def list_cmd(ctx: typer.Context) -> None:
    """List secret references (provider-bound), without values."""
    with services(ctx) as s:
        rows = []
        for record in s.providers.list():
            ref = s.credentials.provider_secret_ref(record.id)
            exists = s.credentials.exists(ref)
            rows.append(
                {
                    "provider": record.alias,
                    "provider_type": record.type,
                    "ref": ref,
                    "present": exists,
                }
            )
        if is_json(ctx):
            emit_json(success_envelope("secrets.list", rows))
            return
        if not rows:
            typer.echo("no providers with secrets")
            return
        for row in rows:
            state = "present" if row["present"] else "missing"
            typer.echo(f"  {row['provider']:<16} {row['provider_type']:<16} {state}")


@app.command("add")
@with_error_handling("secrets.add")
def add(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
    from_env: str = typer.Option(
        None, "--from-env", help="Read the secret from this environment variable."
    ),
) -> None:
    """Attach a secret to a provider. Value via --from-env or hidden prompt."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        if from_env:
            value = os.environ.get(from_env)
            if not value:
                raise InvalidUsageError(f"Environment variable is empty: {from_env}")
        else:
            value = typer.prompt("Secret", hide_input=True, confirmation_prompt=True)
        ref = s.credentials.store_provider_secret(record.id, value)
        data = {"provider": record.alias, "ref": ref, "present": s.credentials.exists(ref)}
        if is_json(ctx):
            emit_json(success_envelope("secrets.add", data))
        else:
            typer.echo(f"secret stored for {record.alias} -> {ref}")


@app.command("remove")
@with_error_handling("secrets.remove")
def remove(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
) -> None:
    """Remove a provider's stored secret."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        ref = s.credentials.provider_secret_ref(record.id)
        removed = s.credentials.delete(ref)
        if not removed:
            raise NotFoundError(f"No secret stored for {record.alias}")
        if is_json(ctx):
            emit_json(success_envelope("secrets.remove", {"provider": record.alias}))
        else:
            typer.echo(f"removed secret for {record.alias}")


@app.command("rotate")
@with_error_handling("secrets.rotate")
def rotate(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
    from_env: str = typer.Option(
        None, "--from-env", help="Read the new secret from this environment variable."
    ),
) -> None:
    """Replace a provider's stored secret with a new value."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        if not s.credentials.exists(s.credentials.provider_secret_ref(record.id)):
            raise NotFoundError(
                f"No secret stored for {record.alias}", hint="Use `rinari secrets add`."
            )
        if from_env:
            value = os.environ.get(from_env)
            if not value:
                raise InvalidUsageError(f"Environment variable is empty: {from_env}")
        else:
            value = typer.prompt("New secret", hide_input=True, confirmation_prompt=True)
        s.credentials.store_provider_secret(record.id, value)
        if is_json(ctx):
            emit_json(success_envelope("secrets.rotate", {"provider": record.alias}))
        else:
            typer.echo(f"rotated secret for {record.alias}")


@app.command("scopes")
@with_error_handling("secrets.scopes")
def scopes(ctx: typer.Context) -> None:
    """Show which providers are bound to a secret reference."""
    with services(ctx) as s:
        rows = []
        for record in s.providers.list():
            ref = s.credentials.provider_secret_ref(record.id)
            rows.append(
                {"provider": record.alias, "ref": ref, "present": s.credentials.exists(ref)}
            )
        if is_json(ctx):
            emit_json(success_envelope("secrets.scopes", rows))
            return
        for row in rows:
            typer.echo(f"  {row['provider']:<16} {row['ref']}")


@app.command("test")
@with_error_handling("secrets.test")
def test(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
) -> None:
    """Check that a provider's secret resolves (value is never printed)."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        ref = s.credentials.provider_secret_ref(record.id)
        try:
            value = s.credentials.resolve(ref)
            ok = bool(value)
        except Exception:
            ok = False
        data = {"provider": record.alias, "resolvable": ok}
        if is_json(ctx):
            emit_json(success_envelope("secrets.test", data))
            return
        typer.echo(f"secret for {record.alias}: {'resolvable' if ok else 'NOT resolvable'}")


def _find_provider(s, alias: str):
    for record in s.providers.list():
        if record.alias == alias:
            return record
    raise NotFoundError(f"Provider not found: {alias}", hint="See `rinari providers list`.")


__all__ = ["SecretRef", "app"]
