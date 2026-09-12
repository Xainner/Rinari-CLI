"""Production SubagentRunner: scoped AgentLoop per subagent (phase 6).

Children inherit the parent catalog and permission ceiling. Explicit agent
restrictions narrow that scope. Approvals route through the parent's consent
surface; budgets and cancellation remain linked to the spawning turn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    parent_runtime: Any = None  # late-bound parent ToolRuntime
    activity_sink: Any = None
    project_instructions: Any = ()
    parent_profile: str = "workspace"
    caller_for: Any = None  # (agent_name) -> ModelCaller | None; None = inherit
    effort_for: Any = None  # (agent_name) -> effort str | None; None = inherit


class _SubagentRunner:
    def __init__(self, config: SubagentRuntimeConfig) -> None:
        self._config = config

    def run(self, spec: SubagentRunSpec) -> AgentResult:
        cfg = self._config
        definition = spec.definition
        tool_ctx = self._build_tool_ctx(spec)
        self._activity(
            spec, "agent.context", {"cwd": str(tool_ctx.cwd), "profile": tool_ctx.profile.value}
        )
        registry = self._build_registry(spec)
        runtime = ToolRuntime(
            registry,
            cfg.policy,
            self._approvals(spec),
            clock=cfg.parent_session_ctx.clock,
            event_sink=(
                (lambda event, payload: cfg.event_sink(spec.session_id, event, payload))
                if cfg.event_sink is not None
                else None
            ),
        )
        # The policy scope is derived from tool_ctx (policy is enforced at
        # runtime, not by prompt text): profile here is the isolation key.
        # Per-agent model override (Phase 7): assigned caller wins, None inherits.
        caller = cfg.caller
        if cfg.caller_for is not None:
            try:
                override = cfg.caller_for(definition.name)
            except Exception:
                override = None
            if override is not None:
                caller = override
        # Per-agent effort (docs/desktop 03-A): stored config reaching the
        # existing reasoning_effort call path; None inherits session effort.
        effort = None
        if callable(getattr(cfg, "effort_for", None)):
            try:
                effort = cfg.effort_for(definition.name)
            except Exception:
                effort = None
        loop = AgentLoop(
            caller,
            runtime,
            _make_assembler(),
            event_sink=cfg.event_sink,
            hook_sink=cfg.hook_sink,
            activity_sink=lambda event, payload: self._activity(spec, event, payload),
            reasoning_effort=effort,
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

        import contextlib

        if tool_ctx.browser is not None:
            with contextlib.suppress(Exception):
                tool_ctx.browser.close()
        for process in tool_ctx.processes.list():
            with contextlib.suppress(Exception):
                tool_ctx.processes.kill(process)
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
        parent = self._config.parent_runtime() if self._config.parent_runtime else None
        source = parent.registry if parent is not None else self._config.base_registry
        tools = (
            [source.get(name) for name in source.names()]
            if source is not None
            else all_native_tools()
        )
        for tool in tools:
            if tool is None or tool.name.startswith(("agent.", "channel.", "capability.")):
                continue
            # Artifact import is the channel-bound half of attachment
            # delivery. Children must not inherit it with a stale host binding.
            if tool.name == "artifact.import":
                continue
            if definition.allows(tool.name):
                registry.register(tool)
        # Discovery closures must search the restricted child catalog.
        from rinari.capability_search import capability_activation_tools, capability_search_tool

        for tool in [capability_search_tool(registry), *capability_activation_tools(registry)]:
            if definition.allows(tool.name):
                registry.register(tool)
        return registry

    def _build_tool_ctx(self, spec: SubagentRunSpec):
        cfg = self._config
        parent_ctx = cfg.parent_session_ctx
        from rinari.tools.native.process import ProcessRegistry

        definition = spec.definition
        worktree_path: Path | None = spec.worktree.path if spec.worktree else None
        cwd = worktree_path or (spec.project_root or parent_ctx.cwd)
        profile = parent_ctx.profile
        rank = {
            PermissionProfile.READ_ONLY: 0,
            PermissionProfile.WORKSPACE: 1,
            PermissionProfile.FULL_ACCESS: 2,
        }
        if definition.profile != "inherit":
            requested = PermissionProfile(definition.profile)
            if rank[requested] < rank[profile]:
                profile = requested
        from rinari.policy.sandbox import FilesystemSandbox
        from rinari.tools.exposure import ToolExposure

        parent_sandbox = parent_ctx.sandbox
        sandbox = FilesystemSandbox(
            read_root=cwd if worktree_path else parent_sandbox.read_root,
            write_roots=()
            if profile == PermissionProfile.READ_ONLY
            else ((cwd,) if worktree_path else parent_sandbox.write_roots),
            unrestricted=profile == PermissionProfile.FULL_ACCESS and parent_sandbox.unrestricted,
            unrestricted_reads=parent_sandbox.unrestricted_reads or parent_sandbox.unrestricted,
            approved_read_roots=parent_sandbox.approved_read_roots,
        )
        token = _LinkedToken(spec.token, getattr(parent_ctx, "cancellation", None))
        from rinari.browser import BrowserManager
        from rinari.tools.activity import activity_output_sink

        browser = BrowserManager(
            session_id=f"{spec.session_id}-{spec.agent_id}",
            home_root=(parent_ctx.user_home or parent_ctx.artifact_root) / ".rinari",
        )
        return replace(
            parent_ctx,
            session_id=f"{spec.session_id}::{spec.agent_id}",
            cwd=cwd,
            project_root=spec.project_root,
            profile=profile,
            sandbox=sandbox,
            cancellation=token,
            processes=ProcessRegistry(),
            exposure=ToolExposure(),
            browser=browser,
            pty=None,
            channel_host=None,
            memory_source=None,
            ask_user=None,
            web_snapshots={},
            change_tracker=None,
            worktree=None,
            output_sink=activity_output_sink(
                lambda event, payload: self._activity(spec, event, payload)
            ),
        )

    def _approvals(self, spec):
        parent = self._config.parent_runtime() if self._config.parent_runtime else None
        if parent is None:
            return ApprovalEngine(prompt=None)
        cancellation = _LinkedToken(spec.token, self._config.parent_token)

        class InheritedApprovals:
            def check(self, request):
                return parent.approvals.check(
                    replace(
                        request,
                        session_id=spec.session_id,
                        cancellation=cancellation,
                        description=(
                            f"[{spec.definition.name} · {spec.agent_id}] {request.description}"
                        ),
                    )
                )

        return InheritedApprovals()

    def _activity(self, spec, event, payload):
        if self._config.activity_sink is not None:
            self._config.activity_sink(
                "agent.activity",
                {
                    **payload,
                    "agent_id": spec.agent_id,
                    "agent": spec.definition.name,
                    "child_event": event,
                    "objective": spec.objective,
                },
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
