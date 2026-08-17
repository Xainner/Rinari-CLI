"""CLI wiring for the agent runtime (phase 2).

Bridges the storage/application layer (SessionRecord, ServiceContainer) with
the provider-agnostic AgentLoop: builds the sandbox, tool context, redactor,
approval prompt, and event persistence for one session. The conversation
itself stays in runtime/agent.py; this module only wires and renders.

In-session provider/model switches (harness.md 86/87) update the *session
record only* - they never touch the global active selection (config_values),
so switching here can never delete or reorder saved providers/models.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import typer

from rinari import __version__
from rinari.application.services import ServiceContainer
from rinari.models.router import ModelRouter
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.prompts.assembler import AssemblerContext, ProjectInstruction, PromptAssembler
from rinari.runtime.agent import AgentContext, AgentLoop, TurnResult
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.model_caller import ModelCaller
from rinari.shared.clock import now_iso
from rinari.shared.errors import CancelledError, InvalidUsageError
from rinari.shared.redaction import Redactor
from rinari.storage.records import SessionEventRecord, SessionRecord
from rinari.tools.definition import ToolContext
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PROJECT_INSTRUCTION_FILES = ("RINARI.md", "AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTION_BYTES = 32 * 1024


@dataclass
class AgentSession:
    """Everything one session needs to run turns, plus in-session switches."""

    services: ServiceContainer
    record: SessionRecord
    caller: ModelCaller
    loop: AgentLoop
    context: AgentContext
    token: CancellationToken = field(default_factory=CancellationToken)


@dataclass(frozen=True, slots=True)
class ProviderSwitchResult:
    provider_alias: str
    model_alias: str | None


# ---------------------------------------------------------------------------
# Segment loaders (trusted content only; no user/project data here)
# ---------------------------------------------------------------------------


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def project_instructions(root: Path | None) -> tuple[ProjectInstruction, ...]:
    """Load RINARI.md / AGENTS.md / CLAUDE.md from the project root, bounded."""
    if root is None:
        return ()
    found: list[ProjectInstruction] = []
    for name in PROJECT_INSTRUCTION_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()[:MAX_INSTRUCTION_BYTES]
            text = raw.decode("utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            found.append(ProjectInstruction(provenance=f"{root.name}/{name}", content=text))
    return tuple(found)


def build_assembler_context(services: ServiceContainer, record: SessionRecord) -> AssemblerContext:
    constitution = _load_text(ASSETS / "constitution.md")
    soul = _load_text(ASSETS / "soul.md")
    root = Path(record.project_root_snapshot) if record.project_root_snapshot else None
    environment: dict = {"cwd": record.current_cwd, "version": __version__}
    if root is not None:
        environment["project_root"] = str(root)
    return AssemblerContext(
        session_kind=record.kind,
        constitution=constitution,
        runtime_policy=_policy_summary(record.kind),
        soul=soul,
        project_instructions=project_instructions(root),
        environment=environment,
    )


def _policy_summary(kind: str) -> str:
    if kind == "PROJECT":
        return (
            "Runtime policy: you operate inside the project sandbox. Reads and writes "
            "inside the project root are allowed; writes outside it and any shell "
            "command that mutates remote Git require user approval. Sensitive "
            "credential files (.env, keys, credentials) always require approval. "
            "Never reveal or copy secrets into files or logs."
        )
    return (
        "Runtime policy: this is a global chat session with no implicit writable "
        "workspace. File writes and shell commands require user approval; $HOME is "
        "never an implicit writable root. Reads outside the current directory ask "
        "for approval."
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _secrets_for_redaction(services: ServiceContainer) -> list[str]:
    secrets: list[str] = []
    for record in services.providers.list():
        try:
            secret = services.providers.resolve_secret(record)
        except Exception:
            continue
        if secret:
            secrets.append(secret)
    return secrets


def _sandbox_for(record: SessionRecord) -> FilesystemSandbox:
    root = Path(record.project_root_snapshot) if record.project_root_snapshot else None
    if record.kind == "PROJECT" and root is not None:
        return FilesystemSandbox(read_root=root, write_roots=(root,))
    # CHAT (harness.md 76): explicit reads inside the home tree, no implicit
    # broad write root. $HOME writability stays a locked system rule.
    return FilesystemSandbox(read_root=Path.home(), write_roots=())


def _persist_event(
    services: ServiceContainer, session_id: str, event_type: str, payload: dict
) -> None:
    services.ctx.event_repo.insert(
        SessionEventRecord(
            id=services.ctx.ids.new("evt"),
            session_id=session_id,
            seq=services.ctx.event_repo.next_seq(session_id),
            type=event_type,
            payload=payload,
            created_at=now_iso(services.ctx.clock),
        )
    )


def build_agent_session(
    services: ServiceContainer, record: SessionRecord, *, interactive: bool
) -> AgentSession:
    root = Path(record.project_root_snapshot) if record.project_root_snapshot else None
    cwd = Path(record.current_cwd)
    home = Path.home()
    sandbox = _sandbox_for(record)
    token = CancellationToken()
    tools = _build_tools(services, record, interactive=interactive, token=token)
    tool_ctx = ToolContext(
        session_id=record.id,
        kind=record.kind,
        cwd=root if root is not None else cwd,
        project_root=root,
        user_home=home,
        profile=PermissionProfile.WORKSPACE,
        sandbox=sandbox,
        limits=ProcessLimits(timeout_s=300, max_output_bytes=128 * 1024),
        artifact_root=home / ".rinari" / "artifacts" / record.id,
        clock=services.ctx.clock,
        cancellation=token,
    )
    caller = _caller_for(services, record)
    loop = AgentLoop(
        caller,
        tools,
        PromptAssembler(),
        event_sink=lambda sid, t, p: _persist_event(services, sid, t, p),
    )
    context = AgentContext(
        session_id=record.id,
        model_ref=record.model_id,
        tool_ctx=tool_ctx,
        assembler_base=build_assembler_context(services, record),
    )
    return AgentSession(
        services=services, record=record, caller=caller, loop=loop, context=context, token=token
    )


def _build_tools(
    services: ServiceContainer,
    record: SessionRecord,
    *,
    interactive: bool,
    token: CancellationToken,
) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register_all(all_native_tools())

    def ask(request) -> str:
        if not interactive:
            return "n"
        target = f" {request.target}" if request.target else ""
        answer = typer.prompt(
            f"Approve {request.capability}{target} (risk: {request.risk})? [y/s/p/n]",
            default="n",
        ).lower()
        return answer if answer else "n"

    return ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=ask),
        clock=services.ctx.clock,
        redactor=Redactor(_secrets_for_redaction(services)),
        event_sink=lambda event_type, payload: _persist_event(
            services, record.id, event_type, payload
        ),
    )


def _caller_for(services: ServiceContainer, record: SessionRecord) -> ModelCaller:
    router = ModelRouter(services.providers, services.models)
    return ModelCaller(
        router=router,
        provider=services.providers.get(record.provider_id),
        model_id=record.model_id,
    )


# ---------------------------------------------------------------------------
# In-session provider/model switching (session-scoped only, harness.md 86/87)
# ---------------------------------------------------------------------------


def switch_provider(session: AgentSession, ref: str) -> ProviderSwitchResult:
    services = session.services
    provider = services.providers.get(ref)
    models = services.ctx.model_repo.list(provider.id)
    if not models:
        raise InvalidUsageError(
            f"Provider {provider.alias!r} has no saved models",
            hint=f"Add one: `rinari models add --provider {provider.alias} <model>`.",
        )
    current = services.providers.current()
    model_id = (
        current.model.id
        if current is not None
        and current.model is not None
        and current.model.id in {m.id for m in models}
        else models[0].id
    )
    record = _update_selection(services, session.record, provider.id, model_id)
    return _apply_session(services, session, record)


def switch_model(session: AgentSession, ref: str) -> ProviderSwitchResult:
    services = session.services
    model = services.models.resolve(ref)
    provider = services.providers.get(model.provider_id)
    record = _update_selection(services, session.record, provider.id, model.id)
    return _apply_session(services, session, record)


def _update_selection(
    services: ServiceContainer, record: SessionRecord, provider_id: str, model_id: str
) -> SessionRecord:
    updated = replace(record, provider_id=provider_id, model_id=model_id)
    services.ctx.session_repo.update(updated)
    return updated


def _apply_session(
    services: ServiceContainer, session: AgentSession, record: SessionRecord
) -> ProviderSwitchResult:
    session.record = records_get(services, record.id)
    session.caller = _caller_for(services, session.record)
    session.context.model_ref = session.record.model_id or ""
    model_id = session.record.model_id
    model = services.ctx.model_repo.get(model_id) if model_id else None
    return ProviderSwitchResult(
        provider_alias=services.providers.get(session.record.provider_id).alias,
        model_alias=model.alias if model is not None else None,
    )


def records_get(services: ServiceContainer, session_id: str) -> SessionRecord:
    record = services.ctx.session_repo.get(session_id)
    if record is None:
        raise InvalidUsageError(f"Session no longer exists: {session_id}")
    return record


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------


def run_turn(session: AgentSession, message: str, *, on_delta=None, on_tool=None) -> TurnResult:
    try:
        return session.loop.turn(
            session.context,
            message,
            on_delta=on_delta,
            on_tool=on_tool,
            cancel=session.token,
        )
    except CancelledError:
        return TurnResult(kind="cancelled", content="Turn cancelled.", tool_calls=0, usage=None)
