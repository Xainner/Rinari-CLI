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
        trace: dict[str, Any] | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        started_at = now_iso(self._ctx_clock())
        requested = {"tool": tool_name, "arguments": self._redact_payload(arguments)}
        if tool_call_id:
            requested["tool_call_id"] = tool_call_id
        requested.update(trace or {})
        self._event("ToolRequested", requested)
        result = self._execute_inner(tool_name, arguments, ctx, tool_call_id)
        duration_ms = (time.monotonic() - started) * 1000.0
        completed = {
            "tool": tool_name,
            "name": tool_name,
            "ok": result.ok,
            "duration_ms": round(duration_ms, 1),
            # §5.2 determinism metadata: stable ordering anchors per call.
            "started_at": started_at,
            "completed_at": now_iso(self._ctx_clock()),
            "error_code": result.error.code.value if result.error else None,
            "truncated": result.truncated,
            "artifacts": [artifact.uri for artifact in result.artifacts],
        }
        if tool_call_id:
            completed["tool_call_id"] = tool_call_id
        completed.update(trace or {})
        self._event("ToolCompleted" if result.ok else "ToolFailed", completed)
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

        call_ctx = self._deadline_ctx(ctx, tool)
        attempts = 2 if tool.idempotent else 1
        attempt = 0
        while True:
            attempt += 1
            result = self._invoke_handler(tool, arguments, call_ctx, ctx, tool_call_id)
            if result.ok and tool.output_schema:
                # P0.5: the contract is enforced, not decorative — dynamic
                # sources (plugins/MCP/OpenAPI) must honor their schema.
                output_errors = validate_against(tool.output_schema, result.data)
                if output_errors:
                    return self._error(
                        ctx,
                        ToolErrorCode.VALIDATION_FAILED,
                        f"tool {tool_name} output violates its output_schema: "
                        + "; ".join(output_errors[:5]),
                    )
            if result.ok or attempt >= attempts or not self._retryable_result(tool, result):
                break
            if cancellation is not None:
                cancellation.throw_if_cancelled()
        return self._postprocess(result, ctx, tool_call_id, tool)

    # NEVER auto-retry an ambiguous result for these: a duplicate send is
    # worse than surfacing the failure (P0.5).
    _NO_RETRY_SIDE_EFFECTS = frozenset(
        {"communication", "financial", "credential"},
    )
    # Only clearly-transient failures are retried. TIMEOUT is deliberately
    # excluded: the attempt already consumed its full budget (and wait-style
    # tools change state between attempts), so retrying doubles the wait and
    # masks the real error.
    _RETRYABLE_CODES = frozenset(
        {ToolErrorCode.NETWORK_ERROR, ToolErrorCode.RATE_LIMITED},
    )

    def _retryable_result(self, tool: ToolDefinition, result: ToolResult) -> bool:
        if not tool.idempotent:
            return False
        if tool.side_effects in self._NO_RETRY_SIDE_EFFECTS:
            return False
        return (
            result.error is not None
            and result.error.retryable
            and result.error.code in self._RETRYABLE_CODES
        )

    def _deadline_ctx(self, ctx: ToolContext, tool: ToolDefinition) -> ToolContext:
        """Narrow the context deadline to the tool's own timeout (P0.5).

        Sync Python handlers cannot be preempted safely; the deadline is a
        cooperative contract that HTTP/MCP/browser/subprocess/LSP adapters
        must respect, and the remaining enforcement surface.
        """
        if not tool.timeout_ms:
            return ctx
        return dataclasses.replace(ctx, deadline_at=time.time() + tool.timeout_ms / 1000.0)

    def _invoke_handler(
        self,
        tool: ToolDefinition,
        arguments: dict,
        call_ctx: ToolContext,
        ctx: ToolContext,
        tool_call_id: str,
    ) -> ToolResult:
        try:
            result = tool.handler(arguments, call_ctx)
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
        return result

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
            rule_id=decision.rule_id,
            reusable=decision.reusable,
            choices=decision.choices,
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

    def _postprocess(
        self,
        result: ToolResult,
        ctx: ToolContext,
        tool_call_id: str,
        tool: ToolDefinition | None = None,
    ) -> ToolResult:
        truncated = False
        data = result.data
        spill_ref = None
        # P0.5: per-tool cap wins over the global spill threshold.
        cap = self.spill_threshold_bytes
        if tool is not None and tool.max_output_bytes:
            cap = min(cap, tool.max_output_bytes)
        if isinstance(data, (str, bytes)):
            payload = self._redactor.redact(data) if isinstance(data, str) else data
            size = len(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
            if size > cap:
                spill_ref = self._spill(tool_call_id or "tool", payload, ctx)
                truncated = True
            data = payload
        elif isinstance(data, dict):
            data = self._redact_payload(data)
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str) and len(text.encode("utf-8")) > cap:
                spill_ref = self._spill(tool_call_id or "tool", text, ctx)
                truncated = True
                # Keep metadata out of the literal preview. Otherwise a model
                # can feed the spill marker back into an exact-text edit.
                data.pop("text", None)
                data["artifact"] = spill_ref.uri
                data["spill_guidance"] = (
                    "Use artifact.read with start_byte/max_bytes, or use "
                    "fs.read_lines on the source file, before editing."
                )
                data["text_preview"] = text[:1536]
        if spill_ref is not None and not isinstance(data, dict):
            data = {
                "summary": (f"output exceeded {cap} bytes; spilled to artifact"),
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
        directory = ctx.artifact_root / ctx.session_id / "runtime"
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_call_id) or "result"
        suffix = ".bin" if isinstance(payload, bytes) else ".txt"
        path = directory / f"{safe_id}{suffix}"
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        return ArtifactRef(
            uri=f"artifact://{ctx.session_id}/runtime/{path.name}",
            name=path.name,
            kind="tool-output",
        )

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
