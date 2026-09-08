"""Production SubagentRunner: scoped AgentLoop per subagent (phase 6).

Isolation guarantees (harness.md 111-115, AGENTS.md 19):

- tool allowlist: the subagent's ToolRegistry contains only the tools its
  AgentDefinition allows (never the whole parent registry);
- permission profile: read-only agents get PermissionProfile.READ_ONLY
  (any write is denied by the policy engine, not by prompt text);
- writes are confined: writer subagents run in their own worktree, which is
  the only writable root; without a worktree a writer may use the project
  root (single-writer sessions only);
- budgets: per-agent model/tool/wall ceilings via the injected BudgetMeter;
- cancellation: a child token linked to the parent session token
  (propagates both directions: parent cancel kills in-flight subagents);
- approvals: subagents never inherit approval prompts — an ASK decision
  resolves to deny (a subagent cannot silently gain consent the user never
  gave for its actions);
- output: a structured AgentResult (evidence refs, files changed,
  validation block) — reporting, not a new trusted system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.agents.orchestrator import (
    AgentResult,
    SubagentRunSpec,
    extract_evidence,
    parse_validation_block,
)
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


class _LinkedToken(CancellationToken):
    """Cancelled when either its own flag or the parent session token sets."""

    def __init__(self, own: CancellationToken, parent: CancellationToken | None) -> None:
        super().__init__()
        self._own = own
        self._parent = parent

    @property
    def cancelled(self) -> bool:
        return (
            super().cancelled
            or self._own.cancelled
            or (self._parent is not None and self._parent.cancelled)
        )

    def cancel(self) -> None:
        super().cancel()
        self._own.cancel()
        if self._parent is not None:
            self._parent.cancel()

    def throw_if_cancelled(self, message: str = "Operation cancelled") -> None:
        # The base implementation checks only the internal flag; the link
        # must be honored at every throw site (loop boundaries, tools).
        if self.cancelled:
            raise CancelledError(message, hint="The subagent was interrupted.")
        super().throw_if_cancelled(message)


@dataclass
class SubagentRuntimeConfig:
    """Everything the runner needs from the parent session."""

    caller: Any  # ModelCaller
    base_registry: ToolRegistry | None
    parent_token: CancellationToken
    policy: PolicyEngine
    sandbox_factory: Any  # (profile, cwd, write_roots) -> FilesystemSandbox
    parent_session_ctx: Any  # ToolContext of the session (clock, limits, artifact_root...)
    worktrees: Any | None = None  # WorktreeManager | None
    constitution: str = ""
    soul: str = ""
    runtime_policy: str = ""
    event_sink: Any = None
    hook_sink: Any = None
    project_instructions: Any = ()
    parent_profile: str = "workspace"


class _SubagentRunner:
    def __init__(self, config: SubagentRuntimeConfig) -> None:
        self._config = config

    def run(self, spec: SubagentRunSpec) -> AgentResult:
        cfg = self._config
        definition = spec.definition
        tool_ctx = self._build_tool_ctx(spec)
        registry = self._build_registry(spec)
        runtime = ToolRuntime(
            registry,
            cfg.policy,
            ApprovalEngine(prompt=None),  # subagents never prompt; ASK -> deny
            clock=cfg.parent_session_ctx.clock,
            event_sink=(
                (lambda event, payload: cfg.event_sink(spec.session_id, event, payload))
                if cfg.event_sink is not None
                else None
            ),
        )
        # The policy scope is derived from tool_ctx (policy is enforced at
        # runtime, not by prompt text): profile here is the isolation key.
        loop = AgentLoop(
            cfg.caller,
            runtime,
            _make_assembler(),
            event_sink=cfg.event_sink,
            hook_sink=cfg.hook_sink,
        )
        base = self._assembler_base(spec, definition)
        context = AgentContext(
            session_id=f"{spec.session_id}::{spec.agent_id}",
            model_ref=getattr(cfg.parent_session_ctx, "model_ref", "") or "",
            tool_ctx=tool_ctx,
            assembler_base=base,
        )
        parent_budget = getattr(spec, "parent_budget", None)
        if parent_budget is not None:
            # Hierarchical ledger (P0.10): the child's spend forwards to
            # the spawning turn; the spawn itself is counted with depth.
            budget = parent_budget.spawn_child(
                TurnBudgetLimits(
                    max_model_calls=definition.budget.max_model_calls,
                    max_tool_calls=definition.budget.max_tool_calls,
                    max_network_calls=definition.budget.max_tool_calls,
                    max_wall_time_s=definition.budget.max_wall_time_s,
                ),
                depth=spec.depth,
            )
        else:
            budget = BudgetMeter(
                TurnBudgetLimits(
                    max_model_calls=definition.budget.max_model_calls,
                    max_tool_calls=definition.budget.max_tool_calls,
                    max_network_calls=definition.budget.max_tool_calls,
                    max_wall_time_s=definition.budget.max_wall_time_s,
                ),
                clock=cfg.parent_session_ctx.clock,
            )
        # Cancellation propagation: this subagent token reflects both the
        # orchestrator token (agent.cancel) and the parent session token
        # (session `rinari stop` / Ctrl+C), and cancel flows both ways.
        session_token = getattr(cfg.parent_session_ctx, "cancellation", None)
        cancel = _LinkedToken(spec.token, session_token)
        result_kind, content = "failed", ""
        error = ""
        try:
            turn = loop.turn(
                context,
                self._objective_prompt(definition, spec),
                cancel=cancel,
                budget=budget,
            )
            result_kind = turn.kind
            content = turn.content or ""
        except CancelledError:
            result_kind = "cancelled"
            content = ""
        except Exception as exc:
            result_kind = "error"
            content = ""
            error = f"{type(exc).__name__}: {exc}"

        status = {
            "answer": "completed",
            "truncated": "completed",
            "cancelled": "cancelled",
            "budget": "budget",
            "max_model_calls": "budget",
            "loop": "failed",
        }.get(result_kind, "failed")
        if error:
            status = "failed"

        files_changed = self._collect_files(spec, content)
        patch, branch, commit, conflicts = "", None, None, ()
        if spec.worktree is not None and cfg.worktrees is not None and status == "completed":
            try:
                worktree_manager = cfg.worktrees
                committed = worktree_manager.commit_result(
                    spec.worktree, f"{definition.name}: {spec.objective[:80]}"
                )
                branch = spec.worktree.branch
                commit = committed.get("commit")
                patch = worktree_manager.patch(spec.worktree)
                files_changed = (
                    worktree_manager.changed_files_against_base(spec.worktree) or files_changed
                )
            except Exception as exc:
                errors = (error + f" worktree collection failed: {exc}").strip()
                if status == "completed":
                    status = "failed"
                    error = errors

        validation = parse_validation_block(content)
        return AgentResult(
            agent=definition.name,
            objective=spec.objective,
            status=status,
            summary=content.strip()[:4000],
            evidence=tuple(extract_evidence(content)),
            files_changed=tuple(files_changed),
            validation=validation,
            patch=patch,
            branch=branch,
            commit=commit,
            conflicts=conflicts,
            error=error,
            usage=budget.snapshot(),
            provenance=definition.provenance,
        )

    # -- pieces ---------------------------------------------------------------

    def _build_registry(self, spec: SubagentRunSpec) -> ToolRegistry:
        definition = spec.definition
        registry = ToolRegistry()
        for tool in all_native_tools():
            if definition.allows(tool.name):
                registry.register(tool)
        return registry

    def _build_tool_ctx(self, spec: SubagentRunSpec):
        cfg = self._config
        parent_ctx = cfg.parent_session_ctx
        from rinari.policy.sandbox import ProcessLimits
        from rinari.tools.native.process import ProcessRegistry

        definition = spec.definition
        worktree_path: Path | None = spec.worktree.path if spec.worktree else None
        cwd = worktree_path or (spec.project_root or parent_ctx.cwd)
        read_only = definition.profile == "read-only"
        if worktree_path is not None:
            write_roots = (worktree_path,)
        elif read_only:
            write_roots = ()
        else:
            write_roots = (cwd,)
        sandbox = cfg.sandbox_factory(
            PermissionProfile.READ_ONLY if read_only else PermissionProfile.WORKSPACE,
            cwd,
            write_roots,
        )
        from rinari.tools.definition import ToolContext

        parent_token = getattr(parent_ctx, "cancellation", None)
        # The ctx-level token reflects the same cancellation surface as the
        # turn-level linked token: orchestrator cancel or session cancel.
        token = _LinkedToken(spec.token, parent_token)

        return ToolContext(
            session_id=f"{spec.session_id}::{spec.agent_id}",
            kind=parent_ctx.kind,
            cwd=cwd,
            project_root=spec.project_root,
            user_home=parent_ctx.user_home,
            profile=PermissionProfile.READ_ONLY if read_only else PermissionProfile.WORKSPACE,
            sandbox=sandbox,
            limits=ProcessLimits(timeout_s=60, max_output_bytes=128 * 1024),
            artifact_root=parent_ctx.artifact_root,
            clock=parent_ctx.clock,
            cancellation=token,
            processes=ProcessRegistry(),
            web=getattr(parent_ctx, "web", None),
            network=getattr(parent_ctx, "network", None),
            credentials=getattr(parent_ctx, "credentials", None),
            validation=getattr(parent_ctx, "validation", None),
            memory=getattr(parent_ctx, "memory", None),
            context_retrieval=getattr(parent_ctx, "context_retrieval", None),
            lsp=getattr(parent_ctx, "lsp", None),
            project_trusted=getattr(parent_ctx, "project_trusted", False),
        )

    def _assembler_base(self, spec: SubagentRunSpec, definition) -> Any:
        cfg = self._config
        from rinari.prompts.assembler import AssemblerContext

        return AssemblerContext(
            session_kind="PROJECT" if spec.project_root else "CHAT",
            constitution=cfg.constitution,
            runtime_policy=(
                f"You are the {definition.name} subagent (scoped, bounded). "
                f"{definition.objective}\n"
                f"Finish with a ```json validation block "
                f'({{ "tests": "passed|failed|skipped" , "lint": ... }}) when you verified.'
            ),
            soul=cfg.soul,
            project_instructions=cfg.project_instructions,
        )

    @staticmethod
    def _objective_prompt(definition, spec: SubagentRunSpec) -> str:
        lines = [
            f"TASK ({definition.name} subagent):",
            spec.objective,
        ]
        if spec.context:
            lines.append("CONTEXT:")
            for key, value in spec.context.items():
                lines.append(f"- {key}: {value}")
        lines.append(
            "Work strictly within this objective. Report a concise summary with "
            "file:line evidence and the validation block."
        )
        return "\n".join(lines)

    def _collect_files(self, spec: SubagentRunSpec, content: str) -> list[str]:
        refs = extract_evidence(content)
        files: list[str] = []
        for ref in refs:
            if not ref.startswith("artifact://"):
                path_part = ref.split(":", 1)[0] if ":" in ref else ref
                if " " not in path_part:
                    files.append(path_part)
        return sorted(set(files))[:40]


def _make_assembler():
    from rinari.prompts.assembler import PromptAssembler

    return PromptAssembler()


def make_subagent_runner(config: SubagentRuntimeConfig) -> _SubagentRunner:
    return _SubagentRunner(config)


__all__ = ["SubagentRuntimeConfig", "_LinkedToken", "make_subagent_runner"]
