"""Dependency helpers for CLI commands: global params, services, error exits."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer

from rinari.application.context import AppContext, build_app_context
from rinari.application.services import ServiceContainer, build_services
from rinari.cli.output import emit_json, failure_envelope
from rinari.shared.errors import RinariError


@dataclass(slots=True)
class CliParams:
    config: str | None = None
    json_output: bool = False
    no_banner: bool = False
    no_progress: bool = False


_global_json = False


def set_global_json(value: bool) -> None:
    global _global_json
    _global_json = value


def set_params(ctx: typer.Context, params: CliParams) -> None:
    ctx.obj = params


def get_params(ctx: typer.Context | None) -> CliParams:
    obj = ctx.obj if ctx is not None else None
    params = obj if isinstance(obj, CliParams) else CliParams()
    if _global_json and not params.json_output:
        params = CliParams(
            config=params.config,
            json_output=True,
            no_banner=params.no_banner,
            no_progress=params.no_progress,
        )
    return params


def is_json(ctx: typer.Context | None) -> bool:
    return get_params(ctx).json_output


@contextmanager
def app_context(ctx: typer.Context) -> Iterator[AppContext]:
    params = get_params(ctx)
    built = build_app_context(config_path=params.config)
    try:
        yield built
    finally:
        built.close()


@contextmanager
def services(ctx: typer.Context) -> Iterator[ServiceContainer]:
    """App context + shared HTTP client + service container (closed together)."""
    with app_context(ctx) as c:
        client = httpx.Client(timeout=10.0)
        try:
            yield build_services(c, http_client=client, user_home=Path.home())
        finally:
            client.close()


def fail(ctx: typer.Context | None, command: str, err: RinariError) -> NoReturn:
    """Emit a structured failure (envelope with --json, stderr lines otherwise)."""
    if is_json(ctx):
        emit_json(failure_envelope(command, err.message, type(err).machine_code, err.retryable))
    else:
        typer.echo(f"error: {err.message}", err=True)
        if err.hint:
            typer.echo(f"hint: {err.hint}", err=True)
    raise typer.Exit(int(err.code)) from err


def with_error_handling(command: str | None = None) -> Callable:
    """Translate structured errors into stable exit codes + output.

    With ``--json`` the failure is emitted as the machine envelope
    (docs/commands.md section 67); otherwise as short stderr lines.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except RinariError as err:
                ctx = next((a for a in args if isinstance(a, typer.Context)), None)
                fail(ctx, command or func.__name__, err)

        return wrapper

    return decorator
