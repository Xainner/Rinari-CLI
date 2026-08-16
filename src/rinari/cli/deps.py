"""Dependency helpers for CLI commands: global params + context lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any

import typer

from rinari.application.context import AppContext, build_app_context
from rinari.shared.errors import RinariError


@dataclass(slots=True)
class CliParams:
    config: str | None = None


def set_params(ctx: typer.Context, params: CliParams) -> None:
    ctx.obj = params


def get_params(ctx: typer.Context) -> CliParams:
    obj = ctx.obj
    if isinstance(obj, CliParams):
        return obj
    return CliParams()


@contextmanager
def app_context(ctx: typer.Context) -> Iterator[AppContext]:
    params = get_params(ctx)
    built = build_app_context(config_path=params.config)
    try:
        yield built
    finally:
        built.close()


def with_error_handling(func: Callable[..., Any]) -> Callable[..., Any]:
    """Translate structured errors into stable exit codes + stderr messages."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except RinariError as err:
            typer.echo(f"error: {err.message}", err=True)
            if err.hint:
                typer.echo(f"hint: {err.hint}", err=True)
            raise typer.Exit(int(err.code)) from err

    return wrapper
