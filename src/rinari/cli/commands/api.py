"""`rinari api` group: OpenAPI specs + generated tools (phase 5).

Per docs/commands.md section 47. Generated operations become typed tools
under the normal policy (`network.outbound` + mutation risk by method).
v1 loads JSON specs; YAML needs a prior conversion (decision record in
plugins/MCP/OpenAPI, phase 5).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError, NotFoundError, RinariError

app = typer.Typer(help="Manage OpenAPI specs (JSON) as typed tools.", no_args_is_help=True)


def _project_scope(scope: str) -> str:
    if scope not in ("global", "project"):
        raise InvalidUsageError("--scope must be global or project")
    return scope


def _require(services_ctx, name: str, scope: str) -> dict | None:
    row = services_ctx.api.show(name, scope)
    if row is None and scope == "global":
        row = services_ctx.api.show(name, "project")
    return row


@app.command("list")
@with_error_handling("api.list")
def api_list(ctx: typer.Context) -> None:
    """List registered API specs."""
    with services(ctx) as s:
        rows = s.api.list()
        if is_json(ctx):
            emit_json(success_envelope("api.list", {"specs": rows}))
            return
        if not rows:
            typer.echo("No API specs registered (rinari api add <name> path/to/spec.json).")
            return
        typer.echo(f"{'NAME':<18} {'SCHEMA':<8} {'ORIGIN':<6} {'ENABLED':<8} PATH/URL")
        for row in rows:
            target = row.get("path") or row.get("url") or "-"
            typer.echo(
                f"{row['name']:<18} {row['scope']:<8} {row['origin']:<6} "
                f"{'yes' if row['enabled'] else 'no':<8} {target}"
            )


@app.command("add")
@with_error_handling("api.add")
def api_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    path: str = typer.Argument(..., help="Path to a JSON OpenAPI spec."),
    scope: str = typer.Option("global", "--scope"),
    auth: list[str] = typer.Option(None, "--auth", help="SCHEME=env://VAR (repeatable)."),
    override: list[str] = typer.Option(
        None,
        "--override",
        help="OP_ID=risk (low|medium|high) per operation (repeatable).",
    ),
) -> None:
    """Register a JSON OpenAPI spec (previews the operations it generates)."""
    _project_scope(scope)
    auth_map: dict[str, str] = {}
    for item in auth or []:
        if "=" not in item:
            raise InvalidUsageError(f"--auth expected SCHEME=env://VAR, got {item!r}")
        key, ref = item.split("=", 1)
        auth_map[key.strip()] = ref.strip()
    overrides: dict[str, dict] = {}
    for item in override or []:
        if "=" not in item:
            raise InvalidUsageError(f"--override expected OP_ID=risk, got {item!r}")
        op_id, risk = item.split("=", 1)
        overrides[op_id.strip()] = {"risk": risk.strip()}
    with services(ctx) as s:
        spec = s.api.add(name, Path(path), scope=scope, overrides=overrides, auth=auth_map)
        from rinari.openapi.spec import load_spec_file

        doc = load_spec_file(Path(path))
        if is_json(ctx):
            emit_json(
                success_envelope("api.add", {"spec": spec, "operations": len(doc.operations)})
            )
            return
        typer.echo(f"registered spec {name}: {len(doc.operations)} operation(s)")
        for op in doc.operations[:10]:
            typer.echo(f"  {op.method.upper():<7} {op.path}  -> api.{name}.{op.operation_id}")


@app.command("remove")
@with_error_handling("api.remove")
def api_remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """Remove a registered spec and its generated tools."""
    with services(ctx) as s:
        removed = s.api.remove(name, scope)
        if is_json(ctx):
            emit_json(success_envelope("api.remove", {"removed": removed}))
            return
        typer.echo(f"removed {name}" if removed else f"no such spec: {name} ({scope})")


@app.command("show")
@with_error_handling("api.show")
def api_show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """Show a spec record (auth refs are env:// only; secrets are never stored)."""
    with services(ctx) as s:
        row = _require(s, name, scope)
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        if is_json(ctx):
            emit_json(success_envelope("api.show", row))
            return
        typer.echo(f"name:    {row['name']}")
        typer.echo(f"origin:  {row['origin']} {row.get('path') or row.get('url')}")
        typer.echo(f"scope:   {row['scope']}")
        typer.echo(f"enabled: {'yes' if row['enabled'] else 'no'}")
        auth = row.get("auth") or {}
        if auth:
            typer.echo(f"auth:    {json.dumps(auth, sort_keys=True)}")
        overrides = row.get("overrides") or {}
        if overrides:
            typer.echo(f"overrides: {json.dumps(overrides, sort_keys=True)}")


@app.command("validate")
@with_error_handling("api.validate")
def api_validate(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """Re-validate the spec on disk (catches drift after `add`)."""
    with services(ctx) as s:
        row = _require(s, name, scope)
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        data = s.api.validate(name, scope)
        if is_json(ctx):
            emit_json(success_envelope("api.validate", data))
            return
        if data.get("ok"):
            typer.echo(f"OK: {data['operations']} operation(s)")
        else:
            typer.echo(f"FAIL [{data.get('code')}]: {data.get('message')}")


@app.command("auth")
@with_error_handling("api.auth")
def api_auth(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Show the auth requirements detected in the spec (schemes + refs)."""
    with services(ctx) as s:
        row = _require(s, name, "global")
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        data = s.api.show(name, row.get("scope") or "global")
        if is_json(ctx):
            emit_json(success_envelope("api.auth", {"auth_ref": data.get("auth") or {}}))
            return
        auth = data.get("auth") or {}
        if not auth:
            typer.echo("No security requirements resolved (or none detected).")
            return
        for scheme, ref in sorted(auth.items()):
            typer.echo(f"{scheme}: {ref}")


@app.command("tools")
@with_error_handling("api.tools")
def api_tools(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """List the generated tools for a spec."""
    with services(ctx) as s:
        row = _require(s, name, scope)
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        defs = s.api.tool_definitions(name, scope)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "api.tools",
                    {
                        "name": name,
                        "tools": [
                            {
                                "name": d.name,
                                "method": d.manifest.get("method"),
                                "path": d.manifest.get("path"),
                                "risk": d.risk,
                                "side_effects": d.side_effects,
                            }
                            for d in defs
                        ],
                    },
                )
            )
            return
        for d in defs:
            typer.echo(
                f"{d.name}  {d.manifest.get('method', '').upper():<7} "
                f"risk={d.risk:<6} {d.description[:70]}"
            )


@app.command("refresh")
@with_error_handling("api.refresh")
def api_refresh(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    scope: str = typer.Option("global", "--scope"),
) -> None:
    """Reload the spec from its origin and re-derive tools."""
    with services(ctx) as s:
        row = _require(s, name, scope)
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        data = s.api.refresh(name, scope)
        if is_json(ctx):
            emit_json(success_envelope("api.refresh", data))
            return
        typer.echo(f"refreshed {name}: {data['operations']} operation(s)")


@app.command("test")
@with_error_handling("api.test")
def api_test(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    operation: str = typer.Argument(..., help="operationId to call."),
    args: str = typer.Option("{}", "--args", help="JSON arguments."),
) -> None:
    """Call one operation directly (bypasses the model; policy still applies)."""
    try:
        payload = json.loads(args)
    except json.JSONDecodeError as exc:
        raise InvalidUsageError(f"--args must be JSON: {exc}") from None
    with services(ctx) as s:
        row = _require(s, name, "global")
        if row is None:
            raise RinariError(f"api spec not found: {name}")
        op = next(
            (o for o in (s.api._load(row).operations) if o.operation_id == operation),
            None,
        )
        if op is None:
            raise NotFoundError(f"operation not found: {operation}")
        result = s.api.invoke(name, op, payload, _test_env())
        if is_json(ctx):
            emit_json(success_envelope("api.test", result))
            return
        typer.echo(f"HTTP {result.get('status')}")
        typer.echo(json.dumps(result.get("body"), ensure_ascii=False)[:1000])


def _test_env():
    import httpx

    from rinari.openapi.service import _InvocationEnv

    return _InvocationEnv(client=httpx.Client(timeout=30.0), network_guard=None, timeout_s=30.0)


__all__ = ["app"]
