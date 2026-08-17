"""Tool Runtime pipeline (harness.md section 43).

Every tool invocation - native, plugin, MCP, or OpenAPI - passes through:
lookup -> schema validation -> classify -> policy check -> approval gate
-> sandboxed execution -> result normalization -> redaction -> event
persistence -> artifact spill -> bounded result. No tool bypasses it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.policy.approvals import ApprovalEngine, ApprovalRequest
from rinari.policy.engine import CAPABILITY_NETWORK, PolicyAction, PolicyEngine, SessionScope
from rinari.policy.sandbox import FilesystemSandbox
from rinari.shared.clock import Clock, SystemClock, now_iso
from rinari.shared.errors import (
    ApprovalDeniedError,
    CancelledError,
    PermissionDeniedError,
    RinariError,
    SandboxViolationError,
    ToolError,
)
from rinari.shared.redaction import Redactor
from rinari.tools.definition import (
    ArtifactRef,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.registry import ToolRegistry
from rinari.tools.schema import validate_against

EventSink = Callable[[str, dict], None]
# Network audit hook: (session_id, tool, host, action, reason) per gate
# decision on a network.outbound action (network_events table, phase 4).
NetworkEventLog = Callable[[str, str, str, str, str], None]


def scope_from_context(ctx: ToolContext) -> SessionScope:
    return SessionScope(
        kind=ctx.kind,
        root=ctx.project_root if ctx.kind == "PROJECT" else ctx.cwd,
        cwd=ctx.cwd,
        profile=ctx.profile,
        user_home=ctx.user_home,
        worktree=ctx.worktree,
    )


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalEngine,
        *,
        clock: Clock | None = None,
        redactor: Redactor | None = None,
        event_sink: EventSink | None = None,
        spill_threshold_bytes: int = 64 * 1024,
        network_event_log: NetworkEventLog | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self._clock = clock
        self._redactor = redactor or Redactor()
        self._emit = event_sink
        self.spill_threshold_bytes = spill_threshold_bytes
        self._network_event_log = network_event_log

    # ------------------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        arguments: dict,
        ctx: ToolContext,
        *,
        tool_call_id: str = "",
    ) -> ToolResult:
        started = time.monotonic()
        started_at = now_iso(self._ctx_clock())
        self._event(
            "ToolRequested",
            {"tool": tool_name, "arguments": self._redact_payload(arguments)},
        )
        result = self._execute_inner(tool_name, arguments, ctx, tool_call_id)
        duration_ms = (time.monotonic() - started) * 1000.0
        self._event(
            "ToolCompleted" if result.ok else "ToolFailed",
            {
                "tool": tool_name,
                "ok": result.ok,
                "duration_ms": round(duration_ms, 1),
                "error_code": result.error.code.value if result.error else None,
            },
        )
        return dataclasses.replace(
            result,
            tool_call_id=tool_call_id or result.tool_call_id,
            duration_ms=duration_ms,
            timestamp=result.timestamp or started_at,
        )

    # ------------------------------------------------------------------

    def _execute_inner(
        self,
        tool_name: str,
        arguments: dict,
        ctx: ToolContext,
        tool_call_id: str,
    ) -> ToolResult:
        tool = self.registry.get(tool_name)
        if tool is None:
            return self._error(
                ctx,
                ToolErrorCode.TOOL_NOT_FOUND,
                f"Unknown tool: {tool_name}",
                retryable=False,
                details=", ".join(self.registry.names()),
            )
        if tool.handler is None:
            return self._error(ctx, ToolErrorCode.UNKNOWN, f"Tool {tool_name} has no handler")

        errors = validate_against(tool.input_schema, arguments)
        if errors:
            return self._error(ctx, ToolErrorCode.INVALID_ARGUMENT, "; ".join(errors[:5]))

        action = tool.classify_action(arguments)
        scope = scope_from_context(ctx)
        self._event("PolicyChecked", {"tool": tool_name, "capability": action.capability})
        decision = self.policy.decide(
            action.capability,
            scope,
            path=action.fs_path,
            command=action.command,
            host=action.target if action.capability == CAPABILITY_NETWORK else None,
            risk=tool.risk,
            risk_class=tool.side_effects,
        )
        self._event(
            "PolicyDecision",
            {
                "tool": tool_name,
                "capability": action.capability,
                "action": decision.action.value,
                "reason": decision.reason,
            },
        )
        if action.capability == CAPABILITY_NETWORK and self._network_event_log is not None:
            with contextlib.suppress(Exception):  # audit must never sink the tool call
                self._network_event_log(
                    ctx.session_id,
                    tool_name,
                    decision.target or str(action.target or ""),
                    decision.action.value,
                    decision.reason,
                )
        if decision.action is PolicyAction.DENY:
            return self._error(ctx, ToolErrorCode.POLICY_DENIED, decision.reason)
        if decision.action is PolicyAction.ASK:
            granted, ctx = self._request_approval(tool, decision, ctx, tool_call_id)
            if not granted:
                return self._error(ctx, ToolErrorCode.APPROVAL_DENIED, decision.reason)

        cancellation = ctx.cancellation
        if cancellation is not None:
            cancellation.throw_if_cancelled()

        try:
            result = tool.handler(arguments, ctx)
        except CancelledError:
            return self._error(ctx, ToolErrorCode.CANCELLED, "Tool execution cancelled")
        except SandboxViolationError as exc:
            return self._error(ctx, ToolErrorCode.SANDBOX_VIOLATION, exc.message)
        except PermissionDeniedError as exc:
            return self._error(ctx, ToolErrorCode.PERMISSION_DENIED, exc.message)
        except ApprovalDeniedError as exc:
            return self._error(ctx, ToolErrorCode.APPROVAL_DENIED, exc.message)
        except ToolError as exc:
            code = (
                ToolErrorCode(exc.machine_code)
                if exc.machine_code in ToolErrorCode._value2member_map_
                else ToolErrorCode.UNKNOWN
            )
            return self._error(ctx, code, exc.message, retryable=exc.retryable)
        except RinariError as exc:
            return self._error(ctx, ToolErrorCode.UNKNOWN, exc.message)
        except OSError as exc:
            return self._error(
                ctx,
                ToolErrorCode.NOT_FOUND,
                f"filesystem operation failed: {exc.__class__.__name__}",
            )

        if not isinstance(result, ToolResult):
            result = ToolResult(ok=True, data=result)
        return self._postprocess(result, ctx, tool_call_id)

    # ------------------------------------------------------------------

    def _request_approval(
        self,
        tool: ToolDefinition,
        decision,
        ctx: ToolContext,
        tool_call_id: str,
    ) -> tuple[bool, ToolContext]:
        request = ApprovalRequest(
            capability=decision.capability,
            description=f"{tool.name}: {decision.reason}",
            target=decision.target,
            risk=decision.risk,
            session_id=ctx.session_id,
            project_id=None,
        )
        outcome = self.approvals.check(request)
        self._event(
            "ToolApproved" if outcome.granted else "ApprovalDenied",
            {
                "tool": tool.name,
                "capability": decision.capability,
                "reason": outcome.reason,
                "scope": outcome.grant.scope.value if outcome.grant else None,
            },
        )
        if not outcome.granted:
            return False, ctx
        # A granted write outside the base roots extends this call's sandbox
        # for exactly this action's target directory.
        if decision.capability == "fs.write" and decision.target:
            target = Path(decision.target)
            if target.is_absolute() and not self._inside_any(target, ctx.sandbox.write_roots):
                approved_root = target.parent
                sandbox = FilesystemSandbox(
                    ctx.sandbox.read_root, (*ctx.sandbox.write_roots, approved_root)
                )
                return True, dataclasses.replace(ctx, sandbox=sandbox)
        return True, ctx

    @staticmethod
    def _inside_any(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    # ------------------------------------------------------------------

    def _postprocess(self, result: ToolResult, ctx: ToolContext, tool_call_id: str) -> ToolResult:
        truncated = False
        data = result.data
        spill_ref = None
        if isinstance(data, (str, bytes)):
            payload = self._redactor.redact(data) if isinstance(data, str) else data
            size = len(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
            if size > self.spill_threshold_bytes:
                spill_ref = self._spill(tool_call_id or "tool", payload, ctx)
                truncated = True
            data = payload
        elif isinstance(data, dict):
            data = self._redact_payload(data)
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str) and len(text.encode("utf-8")) > self.spill_threshold_bytes:
                spill_ref = self._spill(tool_call_id or "tool", text, ctx)
                truncated = True
                data["text"] = f"[{len(text)} bytes spilled to {spill_ref.uri}] " + text[:512]
        if spill_ref is not None and not isinstance(data, dict):
            data = {
                "summary": (
                    f"output exceeded {self.spill_threshold_bytes} bytes; spilled to artifact"
                ),
                "artifact": spill_ref.uri,
            }
        artifacts = (*result.artifacts, spill_ref) if spill_ref else result.artifacts
        return dataclasses.replace(
            result,
            data=data,
            artifacts=tuple(artifacts),
            truncated=result.truncated or truncated,
        )

    def _spill(self, tool_call_id: str, payload: str | bytes, ctx: ToolContext) -> ArtifactRef:
        directory = ctx.artifact_root / ctx.session_id
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_call_id) or "result"
        path = directory / f"{safe_id}.txt"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        return ArtifactRef(uri=f"file://{path}", name=path.name, kind="tool-output")

    # ------------------------------------------------------------------

    def _error(
        self,
        ctx: ToolContext,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Any = None,
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(code=code, message=message, retryable=retryable, details=details),
        )

    def _event(self, type_: str, payload: dict) -> None:
        if self._emit is not None:
            self._emit(type_, self._redact_payload(payload))

    def _redact_payload(self, payload):
        if isinstance(payload, str):
            return self._redactor.redact(payload)
        if isinstance(payload, dict):
            return {k: self._redact_payload(v) for k, v in payload.items()}
        if isinstance(payload, list):
            return [self._redact_payload(v) for v in payload]
        return payload

    def _ctx_clock(self) -> Clock:
        return self._clock if self._clock is not None else SystemClock()

    def to_iso(self) -> str:
        import datetime

        return datetime.datetime.now(datetime.UTC).isoformat()
