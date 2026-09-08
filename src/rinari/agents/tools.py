"""Model-facing orchestrator tools (phase 6): agent.spawn/wait/... (phase 6).

Tool semantics (TODO.md "Agent Orchestrator"):

    agent.spawn      start a bounded subagent (budget/depth/concurrency enforced)
    agent.wait       block until the agent reaches a terminal state
    agent.status     non-blocking state
    agent.message    push a follow-up instruction to a running agent
    agent.cancel     cancel (propagates to tools/model call)
    agent.result     the structured AgentResult once terminal
    agent.synthesize merge several results: duplicate work, contradictions,
                     task-graph join

Spawning is classified `state.write`: allowed in workspace profiles, denied
in read-only ones (a review-only session cannot spawn writers or readers).
The orchestrator itself is the source of truth for limits; these tools only
delegate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentToolHost:
    orchestrator: object
    known_agents: tuple[str, ...] = ()


def _agent_tools(host: AgentToolHost):
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )

    orch = host.orchestrator

    def _err(code: str, message: str):
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                ToolErrorCode.RESOURCE_EXHAUSTED
                if code.startswith("LIMIT")
                else ToolErrorCode.UNKNOWN,
                message,
            ),
        )

    def spawn(arguments, ctx):
        agent = str((arguments or {}).get("agent") or "")
        objective = str((arguments or {}).get("objective") or "")
        context = (arguments or {}).get("context") or {}
        if not isinstance(context, dict):
            return _err(None, "context must be an object")
        use_worktree = bool((arguments or {}).get("use_worktree", False))
        try:
            agent_id = orch.spawn(
                agent,
                objective,
                context={str(k): str(v) for k, v in context.items()},
                use_worktree=use_worktree,
                parent_budget=getattr(ctx, "parent_budget", None),
            )
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        data = orch.status(agent_id)
        return ToolResult(
            ok=True,
            data={
                "agent_id": agent_id,
                "agent": agent,
                "state": data["state"],
                "profile": data["profile"],
                "worktree": data["worktree"],
                "limits": {
                    "max_concurrent": orch.max_concurrent,
                    "max_depth": orch.max_depth,
                    "max_total": orch.max_total,
                },
            },
            origin="agents",
        )

    def wait(arguments, ctx):
        agent_id = str((arguments or {}).get("agent_id") or "")
        timeout = float((arguments or {}).get("timeout_s", 300) or 300)
        try:
            result = orch.wait(agent_id, timeout_s=min(timeout, 600))
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        return ToolResult(ok=True, data=_result_dict(result), origin="agents")

    def status(arguments, ctx):
        agent_id = str((arguments or {}).get("agent_id") or "")
        try:
            data = orch.status(agent_id) if agent_id else orch.list()
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        return ToolResult(ok=True, data=data, origin="agents")

    def message(arguments, ctx):
        agent_id = str((arguments or {}).get("agent_id") or "")
        text = str((arguments or {}).get("text") or "")
        if not agent_id or not text:
            return _err(None, "agent_id and text required")
        try:
            accepted = orch.message(agent_id, text)
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        if not accepted:
            return _err(None, "agent not running; message not accepted")
        return ToolResult(ok=True, data={"agent_id": agent_id, "accepted": True}, origin="agents")

    def cancel(arguments, ctx):
        agent_id = str((arguments or {}).get("agent_id") or "")
        if not agent_id:
            return _err(None, "agent_id required")
        try:
            cancelled = orch.cancel(agent_id, reason="coordinator")
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        return ToolResult(
            ok=True, data={"agent_id": agent_id, "cancelled": cancelled}, origin="agents"
        )

    def result(arguments, ctx):
        agent_id = str((arguments or {}).get("agent_id") or "")
        if not agent_id:
            return _err(None, "agent_id required")
        r = orch.result(agent_id)
        if r is None:
            state = orch.status(agent_id)["state"]
            return ToolResult(
                ok=True,
                data={"agent_id": agent_id, "state": state, "result": None},
                origin="agents",
            )
        return ToolResult(ok=True, data=_result_dict(r), origin="agents")

    def synthesize(arguments, ctx):
        ids = (arguments or {}).get("agent_ids") or []
        if not isinstance(ids, list) or not ids:
            return _err(None, "agent_ids: list of agent ids required")
        try:
            report = orch.synthesize([str(i) for i in ids])
        except Exception as exc:
            return _err(getattr(exc, "code", ""), str(exc))
        return ToolResult(ok=True, data=report, origin="agents")

    read = ("state.read",)
    write = ("state.write",)
    return [
        ToolDefinition(
            name="agent.spawn",
            description=(
                "Spawn a specialist subagent (explore, reviewer, debugger, "
                "researcher, implementer, verifier) with a bounded objective. "
                "Returns agent_id; use agent.wait/agent.status to track it. "
                "Concurrent/depth/total limits are enforced."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "objective": {"type": "string"},
                    "context": {"type": "object"},
                    "use_worktree": {"type": "boolean"},
                },
                "required": ["agent", "objective"],
            },
            capabilities=write,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=spawn,
        ),
        ToolDefinition(
            name="agent.wait",
            description="Block until a subagent is terminal, then return its structured result.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "timeout_s": {"type": "number"},
                },
                "required": ["agent_id"],
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=wait,
        ),
        ToolDefinition(
            name="agent.status",
            description="Non-blocking status of one agent (or all when agent_id omitted).",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=status,
        ),
        ToolDefinition(
            name="agent.message",
            description="Send a follow-up instruction to a running subagent (next turn).",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["agent_id", "text"],
            },
            capabilities=write,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=message,
        ),
        ToolDefinition(
            name="agent.cancel",
            description="Cancel a running subagent; propagates to its model call and tools.",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            capabilities=write,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=cancel,
        ),
        ToolDefinition(
            name="agent.result",
            description="Fetch the structured AgentResult of an agent (null while still running).",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=result,
        ),
        ToolDefinition(
            name="agent.synthesize",
            description=(
                "Merge several subagent results: per-agent summaries, "
                "duplicate-work detection, validation contradictions, and the "
                "tasks ready to be marked done."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["agent_ids"],
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=synthesize,
        ),
    ]


def _result_dict(result) -> dict:
    return {
        "agent": result.agent,
        "objective": result.objective,
        "status": result.status,
        "ok": result.ok,
        "summary": result.summary,
        "evidence": list(result.evidence),
        "files_changed": list(result.files_changed),
        "validation": result.validation,
        "patch_present": bool(result.patch),
        "branch": result.branch,
        "commit": result.commit,
        "conflicts": list(result.conflicts),
        "error": result.error,
        "usage": result.usage,
        "provenance": result.provenance,
    }


def agent_tools(host: AgentToolHost):
    return _agent_tools(host)


__all__ = ["AgentToolHost", "agent_tools"]
