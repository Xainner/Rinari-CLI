"""System commands: version, status, doctor, setup, init, completion, help."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import typer

from rinari import __version__
from rinari.build_manifest import get_manifest
from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import provider_dict
from rinari.providers.registry import PROVIDER_TYPES
from rinari.runtime.identity import IdentityAsset, load_constitution, load_soul
from rinari.shared.errors import InvalidUsageError, RinariError

system_app = typer.Typer(help="System commands (registered on the root app).")

UNSUPPORTED_SHELLS_ERR = "Shell {shell!r} not supported. Use bash, zsh, fish, powershell, or pwsh."


# -- version ---------------------------------------------------------------


@system_app.command("version")
@with_error_handling("version")
def version_cmd(ctx: typer.Context) -> None:
    """Show the Build Manifest (commands.md section 8)."""
    with services(ctx) as s:
        manifest = get_manifest(s.ctx)
    data = manifest.to_dict()
    data["python"] = sys.version.split()[0]
    data["home"] = str(s.ctx.home)
    if is_json(ctx):
        emit_json(success_envelope("version", data))
        return
    typer.echo(f"Rinari CLI      {manifest.cli}")
    typer.echo(f"Harness         {manifest.harness}")
    typer.echo(f"Python          {data['python']}")
    db_schema = manifest.db_schema_version
    typer.echo(f"DB schema       {db_schema if db_schema is not None else '-'}")
    typer.echo(f"Config schema   {manifest.config_schema_version}")
    typer.echo(f"Tool protocol   {manifest.tool_protocol}")
    typer.echo(f"Session export  {manifest.session_export}")
    typer.echo(f"Plugin API      {manifest.plugin_api}")
    typer.echo(f"Skill API       {manifest.skill_api}")
    typer.echo(_asset_line("Soul", manifest.soul))
    typer.echo(_asset_line("Constitution", manifest.constitution))
    typer.echo(f"Home            {data['home']}")


def _asset_line(label: str, asset: IdentityAsset) -> str:
    return f"{label:<16} {asset.version} sha256:{asset.sha256[:12]} ({asset.source})"


# -- status ------------------------------------------------------------------


@system_app.command("status")
@with_error_handling("status")
def status_cmd(ctx: typer.Context, compact: bool = typer.Option(False, "--compact")) -> None:
    """Fast operational overview of the current context."""
    with services(ctx) as s:
        cwd = Path.cwd()
        detection = s.sessions.detect(cwd)
        selection = s.providers.current()
        data: dict = {
            "version": __version__,
            "kind": "PROJECT" if detection.project_root is not None else "CHAT",
            "project_root": str(detection.project_root) if detection.project_root else None,
            "marker": detection.marker,
            "cwd": str(cwd),
            "profile": s.ctx.config.active_profile_name(),
            "provider": (
                {"alias": selection.provider.alias, "type": selection.provider.type}
                if selection is not None
                else None
            ),
            "model": (
                {
                    "alias": selection.model.alias,
                    "provider_model_id": selection.model.provider_model_id,
                }
                if selection is not None and selection.model is not None
                else None
            ),
            "session": None,
            "git": None,
        }
        if data["kind"] == "PROJECT" and detection.project_root is not None:
            from rinari.projects.git import git_state

            state = git_state(Path(detection.project_root))
            if state.available:
                data["git"] = {"branch": state.branch, "dirty": state.dirty}
        if selection is not None:
            data["credential_ref"] = s.providers.credential_ref(selection.provider)
        if is_json(ctx):
            emit_json(success_envelope("status", data))
            return
        kind_label = "PROJECT" if data["kind"] == "PROJECT" else "CHAT"
        typer.echo(f"Rinari v{__version__}   {kind_label} / profile {data['profile']}")
        typer.echo("-" * 44)
        if data["kind"] == "PROJECT" and data["project_root"]:
            typer.echo(f"Project   {data['project_root']}  (marker: {data['marker']})")
            git = data.get("git")
            if git is not None:
                typer.echo(f"Git       {git['branch'] or '-'}{' *' if git['dirty'] else ''}")
        else:
            typer.echo(f"cwd       {data['cwd']}")
        provider = data["provider"] or {}
        model = data["model"] or {}
        typer.echo(f"Provider  {provider.get('alias') or '-'} ({provider.get('type') or '-'})")
        if model:
            typer.echo(
                f"Model     {model.get('alias') or '-'} ({model.get('provider_model_id') or '-'})"
            )
        else:
            typer.echo("Model     - (configure with `rinari models add`)")


# -- doctor --------------------------------------------------------------------


def _check(name: str, ok: bool, detail: str = "", level: str = "ok") -> dict:
    return {"check": name, "status": "fail" if not ok else level, "detail": detail}


@system_app.command("doctor")
@with_error_handling("doctor")
def doctor_cmd(ctx: typer.Context) -> None:
    """Run local diagnostics. Never mutates credentials or state."""
    with services(ctx) as s:
        checks: list[dict] = []
        checks.append(
            _check("config.valid", True, f"{len(list(s.ctx.config.layers))} layers merged")
        )
        try:
            s.ctx.db.query_one("SELECT 1 AS ok")
            checks.append(_check("session.database", True, "open (WAL)"))
        except Exception as err:
            checks.append(_check("session.database", False, str(err)))
        layout = s.ctx.layout
        if layout.config_file.is_file():
            checks.append(_check("config.file", True, str(layout.config_file)))
        else:
            checks.append(
                _check(
                    "config.file",
                    True,
                    f"not created yet (no user overrides): {layout.config_file}",
                    level="warn",
                )
            )
        try:
            creds_dir = layout.credentials_dir
            writable = creds_dir.exists() or Path.mkdir(creds_dir, exist_ok=True)
            checks.append(_check("secret.store", bool(writable), str(creds_dir)))
        except OSError as err:
            checks.append(_check("secret.store", False, str(err)))

        providers = s.providers.list()
        if not providers:
            checks.append(_check("providers", True, "none saved", level="warn"))
        for p in providers:
            secret = None
            try:
                secret = s.providers.resolve_secret(p)
            except RinariError as err:
                checks.append(_check(f"provider.{p.alias}", False, err.message))
                continue
            if p.auth_method == "none":
                checks.append(_check(f"provider.{p.alias}", True, "no auth required"))
            elif secret:
                checks.append(
                    _check(f"provider.{p.alias}", True, f"credential resolvable ({p.auth_method})")
                )
            else:
                checks.append(
                    _check(f"provider.{p.alias}", False, "credential reference not resolvable")
                )

        selection = s.providers.current()
        if selection is None:
            checks.append(_check("active.provider", True, "none selected", level="warn"))
        else:
            if selection.model is None:
                checks.append(
                    _check(
                        "active.model",
                        False,
                        f"no model for {selection.provider.alias}",
                        level="warn",
                    )
                )
            else:
                checks.append(
                    _check(
                        "active.model",
                        True,
                        f"{selection.model.alias} on {selection.provider.alias}",
                    )
                )

        checks.append(
            _check(
                "git",
                shutil.which("git") is not None,
                "available" if shutil.which("git") else "git not found",
            )
        )

        for loader in (load_soul, load_constitution):
            try:
                asset = loader(layout.root)
                checks.append(
                    _check(
                        f"identity.{asset.name}",
                        True,
                        f"v{asset.version} {asset.source} sha256:{asset.sha256[:12]}",
                    )
                )
            except RinariError as err:
                checks.append(_check("identity", False, err.message))

        failed = [c for c in checks if c["status"] == "fail"]
        if is_json(ctx):
            emit_json(success_envelope("doctor", {"checks": checks, "failed": len(failed)}))
            return
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        console = Console()
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(style="bold", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        status_style = {"ok": "green", "warn": "yellow", "fail": "red"}
        status_glyph = {"ok": "✓", "warn": "!", "fail": "×"}  # noqa: RUF001
        for c in checks:
            glyph = status_glyph[c["status"]]
            table.add_row(c["check"], Text(glyph, style=status_style[c["status"]]), c["detail"])
        console.print(table)
        if failed:
            console.print(Text(f"{len(failed)} failed", style="red"))
            raise typer.Exit(1)
        console.print(Text("all checks passed", style="green"))


# -- setup ---------------------------------------------------------------------


@system_app.command("setup")
@with_error_handling("setup")
def setup_cmd(
    ctx: typer.Context,
    provider: str = typer.Option(
        None, "--provider", help="Provider type: openai, anthropic, custom."
    ),
    name: str = typer.Option(None, "--name", help="Provider alias."),
    endpoint: str = typer.Option(None, "--endpoint", help="Base URL (custom only)."),
    api_key: str = typer.Option(None, "--api-key", help="API key value."),
    api_key_env: str = typer.Option(
        None, "--api-key-env", help="Environment variable with the API key."
    ),
    no_auth: bool = typer.Option(False, "--no-auth", help="No credential required."),
    model: str = typer.Option(None, "--model", help="Model ID to save and activate."),
    model_name: str = typer.Option(None, "--model-name", help="Alias for the model."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Fail instead of prompting."
    ),
    interactive: bool = typer.Option(
        False, "--interactive", hidden=True, help="Force interactive prompts (testing)."
    ),
) -> None:
    """First-run onboarding. Re-running never erases existing records."""
    with services(ctx) as s:
        existing = s.providers.list()
        if existing and provider is None:
            if is_json(ctx):
                emit_json(
                    success_envelope(
                        "setup",
                        None,
                        warnings=["existing configuration detected; no changes made"],
                    )
                )
                return
            typer.echo("Existing Rinari configuration found - nothing was changed.")
            typer.echo(f"Saved providers: {', '.join(p.alias for p in existing)}")
            typer.echo("Adjust with: rinari providers|models|model use.")
            return
        if provider is None:
            if interactive or (not non_interactive and not is_json(ctx) and _is_terminal()):
                provider, name, endpoint, api_key, api_key_env, no_auth, model, model_name = (
                    _run_setup_wizard(s, existing)
                )
            else:
                raise InvalidUsageError(
                    "Onboarding flags are required when not running on a terminal.",
                    hint=(
                        "Run `rinari setup` in a terminal for the interactive wizard, or:\n"
                        "  rinari setup --provider openai --api-key-env OPENAI_API_KEY "
                        "--model <id> --model-name <alias>\n"
                        "  rinari providers add custom --name <alias> --endpoint <URL> "
                        "--api-key <key>  (+ rinari models add --provider <alias> --model <id>)"
                    ),
                )
        if provider not in PROVIDER_TYPES:
            raise InvalidUsageError(
                f"unknown provider type {provider!r}",
                hint=f"Expected one of: {', '.join(sorted(PROVIDER_TYPES))}",
            )
        record, model_saved = _execute_onboarding(
            s,
            provider,
            name or provider,
            endpoint,
            api_key,
            api_key_env,
            no_auth,
            model,
            model_name,
        )
        data = {
            "provider": provider_dict(record, credential_ref=s.providers.credential_ref(record)),
            "model": model_saved.alias if model_saved else None,
            "active": True,
        }
        if is_json(ctx):
            emit_json(success_envelope("setup", data))
            return
        typer.echo(f"Provider {record.alias!r} saved and active.")
        if model_saved:
            typer.echo(f"Model {model_saved.alias!r} saved and active.")
        typer.echo("Setup complete. Run `rinari` to start a session.")


def _execute_onboarding(
    s,
    provider_type: str,
    alias: str,
    endpoint: str | None,
    api_key: str | None,
    api_key_env: str | None,
    no_auth: bool,
    model: str | None,
    model_name: str | None,
):
    from rinari.application.provider_service import AddProviderInput

    record = s.providers.add(
        AddProviderInput(
            alias=alias,
            provider_type=provider_type,
            auth_method="none" if no_auth else "api-key",
            endpoint=endpoint,
            secret=api_key,
            secret_env=api_key_env,
        )
    )
    model_saved = None
    if model:
        model_saved = s.models.add(record.alias, model, model_name or model)
        s.models.use(model_saved.id, record.alias)
    return record, model_saved


def _is_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _prompt_choice(question: str, options: Sequence[str], default: str) -> str:
    menu = "  ".join(f"[{i}] {o}" for i, o in enumerate(options, start=1))
    while True:
        raw = (
            typer.prompt(f"{question} ({menu})", default=default, show_default=True) or ""
        ).strip()
        if not raw:
            return default
        lowered = raw.lower()
        if lowered in {o.lower() for o in options}:
            return next(o for o in options if o.lower() == lowered)
        if lowered.isdigit() and 1 <= int(lowered) <= len(options):
            return options[int(lowered) - 1]
        typer.echo(f"  Choose one of: {', '.join(options)} (or a number).")


def _run_setup_wizard(
    s, existing
) -> tuple[str, str, str | None, str | None, str | None, bool, str | None, str | None]:
    typer.echo("Rinari first-run setup")
    typer.echo("-" * 40)
    provider_type = _prompt_choice("Provider type", sorted(PROVIDER_TYPES), default="openai")
    spec = PROVIDER_TYPES[provider_type]

    alias = (typer.prompt("Provider alias", default=provider_type) or provider_type).strip()
    if not alias:
        raise InvalidUsageError("Provider alias is required.", hint="Pick a non-empty alias.")

    endpoint = None
    if spec.default_base_url is None:
        while True:
            endpoint = (
                typer.prompt("Endpoint (base URL, e.g. https://api.example.com/v1)") or ""
            ).strip()
            if endpoint.lower().startswith(("http://", "https://")):
                break
            typer.echo("  Endpoint must start with http:// or https://")

    auth_methods = ["api-key", "env"] + (["none"] if "none" in spec.auth_methods else [])
    default_source = (
        "env" if os.environ.get(_DEFAULT_ENV_KEYS.get(provider_type, "")) else "api-key"
    )
    source = _prompt_choice("Authentication source", auth_methods, default=default_source)
    api_key = None
    api_key_env = None
    no_auth = False
    if source == "none":
        no_auth = True
    elif source == "env":
        api_key_env = (
            typer.prompt("Environment variable name", default=_DEFAULT_ENV_KEYS.get(provider_type))
            or ""
        ).strip()
        if not api_key_env:
            raise InvalidUsageError(
                "Environment variable name is required.", hint="Pick a non-empty variable name."
            )
        if not os.environ.get(api_key_env):
            typer.echo(
                f"  note: {api_key_env} is not set in this shell; the provider can still be added."
            )
    else:
        while True:
            api_key = typer.prompt("API key (input hidden)", hide_input=True)
            if (api_key or "").strip():
                api_key = api_key.strip()
                break

    while True:
        model = (typer.prompt("Model ID (e.g. gpt-4o, qwen3.8-27b)") or "").strip()
        if model:
            break
        typer.echo("  Model ID is required - it is the exact identifier the provider expects.")
    model_name = (typer.prompt("Model alias", default=model) or model).strip() or model
    return provider_type, alias, endpoint, api_key, api_key_env, no_auth, model, model_name


_DEFAULT_ENV_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "custom": ""}


# -- init ------------------------------------------------------------------------


@system_app.command("init")
@with_error_handling("init")
def init_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project directory to initialize."),
    force: bool = typer.Option(False, "--force", help="Re-init over existing project files."),
) -> None:
    """Initialize project integration (.rinari/, RINARI.md, project record)."""
    with services(ctx) as s:
        root = Path(path).expanduser().resolve()
        record, created = s.projects.init(root, user_home=Path.home(), force=force)
        # Creating a project is an explicit local act: trust the new root so
        # its instructions load immediately without a second step.
        s.trust.add(root)
        promoted = None
        chat_session = s.sessions.find_promotable_chat_session(root)
        if chat_session is not None:
            promoted = s.sessions.promote(chat_session.id, root)
        data = {
            "project_id": record.id,
            "root": str(root),
            "created_files": created,
            "trusted": True,
            "promoted_session": promoted.id if promoted is not None else None,
        }
        if is_json(ctx):
            emit_json(success_envelope("init", data))
            return
        for f in created:
            typer.echo(f"created  {f}")
        if promoted is not None:
            typer.echo(
                f"Promoted session {promoted.id} to PROJECT (preserving conversation state)."
            )
        typer.echo(f"Project ready: {root}")


# -- completion ---------------------------------------------------------------------


@system_app.command("completion")
@with_error_handling("completion")
def completion_cmd(
    ctx: typer.Context,
    shell: str = typer.Option(None, "--shell", "-s", help="bash, zsh, fish, powershell, or pwsh."),
) -> None:
    """Print (or install later) the shell completion script."""
    from typer._completion_classes import completion_init
    from typer.completion import get_completion_script

    completion_init()
    if shell is None:
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "completion",
                    {"shell": None, "hint": "pass --shell bash|zsh|fish|powershell|pwsh"},
                )
            )
            return
        typer.echo("Usage: rinari completion --shell <bash|zsh|fish|powershell|pwsh>")
        typer.echo("Then source/save the printed script per your shell's docs.")
        raise typer.Exit(2)
    try:
        script = get_completion_script(
            prog_name="rinari", complete_var="_RINARI_COMPLETE", shell=shell
        )
    except Exception:
        raise InvalidUsageError(UNSUPPORTED_SHELLS_ERR.format(shell=shell)) from None
    typer.echo(script)


# -- help -----------------------------------------------------------------------------


@system_app.command("help")
@with_error_handling("help")
def help_cmd(
    ctx: typer.Context,
    topic: list[str] = typer.Argument(None, help="Command to show help for, e.g. 'providers add'."),
) -> None:
    """Show help for a command (defaults to the root help)."""
    import typer.main as _typer_main
    from typer._click.core import Context as _ClickContext

    from rinari.cli.main import app as root_app  # local import: module is loaded by then

    target = _typer_main.get_command(root_app)
    parts = list(topic or ())
    for i, part in enumerate(parts):
        if not hasattr(target, "commands"):
            raise InvalidUsageError(f"{' '.join(parts[:i]) or 'root'} has no subcommands")
        nxt = target.get_command(_ClickContext(target, info_name=part), part)
        if nxt is None:
            known = ", ".join(target.commands)
            raise InvalidUsageError(f"Unknown command: {part}", hint=f"Available: {known}")
        target = nxt
    typer.echo(target.get_help(_ClickContext(target, info_name=target.name or "rinari")))
