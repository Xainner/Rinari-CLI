"""Tool Runtime pipeline (harness.md section 43).

Every tool invocation - native, plugin, MCP, or OpenAPI - passes through:
lookup -> schema validation -> classify -> policy check -> approval gate
-> sandboxed execution -> result normalization -> redaction -> event
persistence -> artifact spill -> bounded result. No tool bypasses it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.policy.approvals import ApprovalEngine, ApprovalRequest
from rinari.policy.engine import (
    CAPABILITY_NETWORK,
    PolicyAction,
    PolicyEngine,
    SessionScope,
)
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
from rinari.shared.redaction import Redactor, redact_value
from rinari.tools.definition import (
    ArtifactRef,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.registry import ToolRegistry
from rinari.tools.schema import (
    coerce_types,
    declared_properties,
    drop_empty_optionals,
    unknown_arguments_message,
    validate_against,
)

EventSink = Callable[[str, dict], None]
# Network audit hook: (session_id, tool, host, action, reason) per gate
# decision on a network.outbound action (network_events table, phase 4).
NetworkEventLog = Callable[[str, str, str, str, str], None]


# Capabilities a peer-originated turn may never exercise on the strength of
# another agent's text. Reads and discovery stay available; acting requires
# the owner to turn the message into a task of their own ("Usar como tarea").
PEER_ORIGIN_DENIED_CAPABILITIES = frozenset(
    {
        "fs.write",
        "shell.exec",
        "process.local",
        "browser.mutate",
        "network.outbound",
        "mcp.call",
        "state.write",
        "agent.delegate",
        "git.local",
    }
)
# A message from another agent session cannot plant a note that the owner
# would later start with one click.
PEER_ORIGIN_DENIED_TOOLS = frozenset(
    {"agent.spawn", "agent.message", "agent.synthesize", "followup.suggest"}
)


# Key of the old «always in chats» grants. A loose chat no longer offers it
# and the ones already stored no longer apply (#56): Settings lists them as
# old so the owner can remove them.
LOOSE_CHATS = "chats"


def grant_scope_key(ctx: ToolContext) -> str:
    """Where an "always" grant lives: the project root (a loose chat has none)."""
    if ctx.kind == "PROJECT" and ctx.project_root is not None:
        return str(Path(ctx.project_root).resolve())
    return LOOSE_CHATS


def scope_from_context(ctx: ToolContext) -> SessionScope:
    return SessionScope(
        kind=ctx.kind,
        root=ctx.project_root if ctx.kind == "PROJECT" else ctx.cwd,
        cwd=ctx.cwd,
        profile=ctx.profile,
        user_home=ctx.user_home,
        worktree=ctx.worktree,
        private_roots=ctx.private_roots,
        read_profile=ctx.read_profile,
        external_content=bool(getattr(ctx.turn_state, "external_content", False)),
        ask_new_destinations=bool(getattr(ctx, "ask_new_destinations", False)),
        extra_roots=tuple(getattr(ctx, "extra_project_roots", ()) or ()),
    )


def _external_source(action, tool) -> str | None:
    """What makes this action's result untrusted outside content, if anything."""
    from rinari.policy.engine import CAPABILITY_MCP_READ, CAPABILITY_MCP_WRITE, is_local_host
    from rinari.policy.network import normalize_host

    if action.capability in (CAPABILITY_MCP_READ, CAPABILITY_MCP_WRITE):
        return f"mcp:{tool.name}"
    if action.capability == CAPABILITY_NETWORK:
        host = normalize_host(action.target)
        if host and not is_local_host(host):
            return host
    return None


# Inline limit for process output (`context.artifact_output_threshold_kb`).
# 16 KiB is ~4k tokens: a whole test or build summary, a stack trace or a
# compiler error block fit, while a 64 KiB log cost ~16k tokens and was
# resent on every later request of the turn. Above it the model sees the head
# and the tail (what ran, and how it ended) plus an artifact.read pointer to
# the complete output.
DEFAULT_OUTPUT_THRESHOLD_BYTES = 16 * 1024
# Room a capped observation keeps for its result_ref/recovery pointer.
_RECOVERY_METADATA_BYTES = 1024

# Tools whose schema comes from outside Rinari keep their own contract.
_DYNAMIC_SOURCES = frozenset({"plugin", "mcp", "openapi"})


def _tool_key(name: str) -> str:
    """A tool name with dots, dashes and case folded: `mcp_X_y` ~ `mcp.X.y`."""
    return name.replace(".", "_").replace("-", "_").casefold()


def resolve_tool_name(name: str, names: list[str]) -> str | None:
    """The one registered tool a misspelled separator means, or None.

    Models write `mcp_Server_tool` for `mcp.Server.tool` (plan 0.4.0 #48).
    Only an exact match after folding separators counts, and only when it is
    unique: two candidates, or none, are never guessed. The resolved tool is
    then validated, classified and approved exactly like its real name.
    """
    key = _tool_key(name)
    matches = [candidate for candidate in names if _tool_key(candidate) == key]
    return matches[0] if len(matches) == 1 else None


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
        spill_threshold_bytes: int = DEFAULT_OUTPUT_THRESHOLD_BYTES,
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
        self._receipts = OrderedDict()
        self._receipt_lock = threading.RLock()
        # ToolRequested / ToolCompleted / ToolFailed. The Engine stores the
        # activity events instead and turns these off (#91); the CLI keeps them.
        self.legacy_events = True

    # ------------------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        arguments: dict,
        ctx: ToolContext,
        *,
        tool_call_id: str = "",
        trace: dict[str, Any] | None = None,
        prepared=None,
    ) -> ToolResult:
        started = time.monotonic()
        if not isinstance(arguments, dict):
            return self._error(
                ctx, ToolErrorCode.INVALID_ARGUMENT, "Tool arguments must be an object"
            )
        started_at = now_iso(self._ctx_clock())
        requested = {"tool": tool_name, "arguments": self._redact_payload(arguments)}
        if tool_call_id:
            requested["tool_call_id"] = tool_call_id
        requested.update(trace or {})
        if self.legacy_events:
            self._event("ToolRequested", requested)
        request_id = arguments.get("request_id")
        tool = self.registry.get(tool_name) or self.registry.get(
            resolve_tool_name(tool_name, self.registry.names()) or ""
        )
        if prepared is not None:
            result = prepared() if callable(prepared) else prepared
        elif request_id and (not isinstance(request_id, str) or len(request_id) > 128):
            result = self._error(ctx, ToolErrorCode.INVALID_ARGUMENT, "request_id must be a string")
        elif request_id and tool and tool.manifest.get("supports_request_id"):
            clean = {k: v for k, v in arguments.items() if k != "request_id"}
            fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
            key = (ctx.session_id, str(ctx.cwd), tool_name, request_id)
            with self._receipt_lock:
                previous = self._receipts.get(key)
                if previous:
                    result = (
                        previous[1]
                        if previous[0] == fingerprint
                        else self._error(
                            ctx,
                            ToolErrorCode.CONFLICT,
                            "request_id was already used with different arguments",
                        )
                    )
                else:
                    result = self._execute_inner(tool_name, clean, ctx, tool_call_id)
                    # A call rejected before running (bad arguments) did nothing:
                    # the corrected retry may reuse its request_id (V2-8, F24).
                    if not (
                        result.error is not None
                        and result.error.code is ToolErrorCode.INVALID_ARGUMENT
                    ):
                        self._receipts[key] = (fingerprint, result)
                    if len(self._receipts) > 256:
                        self._receipts.popitem(last=False)
        else:
            result = self._execute_inner(tool_name, arguments, ctx, tool_call_id)
        if result.full_observation is None:
            result = self._postprocess(result, ctx, tool_call_id, tool)
        duration_ms = (time.monotonic() - started) * 1000.0
        completed = {
            "tool": tool_name,
            "name": tool_name,
            "ok": result.ok,
            "duration_ms": round(duration_ms, 1),
            # Â§5.2 determinism metadata: stable ordering anchors per call.
            "started_at": started_at,
            "completed_at": now_iso(self._ctx_clock()),
            "error_code": result.error.code.value if result.error else None,
            "truncated": result.truncated,
            "artifacts": [artifact.uri for artifact in result.artifacts],
        }
        if tool_call_id:
            completed["tool_call_id"] = tool_call_id
        completed.update(trace or {})
        if self.legacy_events:
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
        prepared = self.prepare(tool_name, arguments, ctx, tool_call_id)
        return prepared() if callable(prepared) else prepared

    def prepare(self, tool_name: str, arguments: dict, ctx: ToolContext, tool_call_id: str):
        """Validate and authorize on the coordinator; return a bounded execution closure."""
        tool = self.registry.get(tool_name)
        resolved_from: str | None = None
        if tool is None:
            resolved = resolve_tool_name(tool_name, self.registry.names())
            if resolved is not None:
                resolved_from, tool_name = tool_name, resolved
                tool = self.registry.get(tool_name)
        if tool is None:
            return self._error(
                ctx,
                ToolErrorCode.TOOL_NOT_FOUND,
                f"Unknown tool: {tool_name}",
                retryable=False,
                details={
                    "suggestions": difflib.get_close_matches(tool_name, self.registry.names(), n=5),
                    "next_action": "Search registered tools with capability.search.",
                },
            )
        if tool.handler is None:
            return self._error(ctx, ToolErrorCode.UNKNOWN, f"Tool {tool_name} has no handler")

        if tool_name.startswith("web.") and arguments.get("source_id"):
            from rinari.tools.native.web import source_snapshot

            snapshot = source_snapshot(ctx, arguments["source_id"])
            if snapshot is None:
                return self._error(
                    ctx, ToolErrorCode.NOT_FOUND, "Source snapshot expired; reopen its URL"
                )
            arguments = {**arguments, "url": snapshot.url}

        notes: tuple[str, ...] = ()
        if resolved_from is not None:
            notes = (f"used tool {tool_name} (received {resolved_from})",)
        if tool.manifest.get("source") not in _DYNAMIC_SOURCES:
            # Optional keys sent empty count as absent (plan 0.4.0 v2, V2-8).
            arguments, emptied = drop_empty_optionals(tool.input_schema, arguments)
            notes = (*notes, *emptied)
        if tool.normalize is not None and tool.manifest.get("source") not in _DYNAMIC_SOURCES:
            try:
                arguments, rewritten = tool.normalize(arguments)
            except ValueError as exc:
                return self._error(ctx, ToolErrorCode.INVALID_ARGUMENT, str(exc))
            notes = (*notes, *rewritten)
        # Lists, objects, booleans and numbers sent as text (plan 0.4.0 #66e):
        # converted only when they parse to exactly the declared type, so the
        # policy below judges the typed call.
        arguments, coerced = coerce_types(tool.input_schema, arguments)
        notes = (*notes, *coerced)
        if notes:
            # Persisted so the variants models send can be measured.
            normalized = {"tool": tool_name, "notes": list(notes)}
            if tool_call_id:
                normalized["tool_call_id"] = tool_call_id
            self._event("ToolArgumentsNormalized", normalized)
        prepared = self._authorize(tool, arguments, ctx, tool_call_id)
        if not notes:
            return prepared
        if callable(prepared):
            return lambda: self._with_notes(prepared(), notes)
        return self._with_notes(prepared, notes)

    def _with_notes(self, result: ToolResult, notes: tuple[str, ...]) -> ToolResult:
        notes = tuple(self._redactor.redact(note) for note in notes)
        full = result.full_observation
        if full is not None:
            # Round projection re-derives the observation from this copy.
            full = dataclasses.replace(full, notes=(*notes, *full.notes))
        return dataclasses.replace(result, notes=(*notes, *result.notes), full_observation=full)

    def _authorize(self, tool: ToolDefinition, arguments: dict, ctx: ToolContext, tool_call_id):
        """Validate, classify and gate already-normalized arguments."""
        tool_name = tool.name
        if tool.manifest.get("source") not in _DYNAMIC_SOURCES:
            # A native tool ignores keys it does not declare. Running anyway
            # hides a wrong call: timeout_ms instead of timeout_s ran with the
            # 60 s default and the model blamed a fixed cap. Reject before any
            # side effect and name the accepted keys so one retry fixes it.
            declared = declared_properties(tool.input_schema)
            unknown = (
                [key for key in arguments if key not in declared] if declared is not None else []
            )
            if unknown:
                return self._error(
                    ctx,
                    ToolErrorCode.INVALID_ARGUMENT,
                    unknown_arguments_message(tool.name, tool.input_schema, unknown),
                    details={"unknown_arguments": unknown, "accepted": sorted(declared or ())},
                )
        errors = validate_against(tool.input_schema, arguments)
        if errors:
            return self._error(ctx, ToolErrorCode.INVALID_ARGUMENT, "; ".join(errors[:5]))

        try:
            actions = tool.classify_actions(arguments)
        except (TypeError, ValueError, KeyError) as exc:
            return self._error(ctx, ToolErrorCode.INVALID_ARGUMENT, str(exc))
        if ctx.origin_kind == "peer" and tool_name in PEER_ORIGIN_DENIED_TOOLS:
            return self._error(
                ctx,
                ToolErrorCode.POLICY_DENIED,
                "a turn started by another agent's message cannot delegate work; "
                "the owner must send this as their own task",
            )
        if ctx.origin_kind == "peer":
            # Se examina la acción completa antes de policy. Una herramienta
            # multi-autoridad como browser.upload clasifica primero fs.read y
            # después browser.mutate; decidir la primera podía abrir una
            # aprobación que jamás debe convertir un mensaje peer en permiso
            # para mutar o exfiltrar. El techo vence al orden de las acciones.
            denied = next(
                (
                    action.capability
                    for action in actions
                    if action.capability in PEER_ORIGIN_DENIED_CAPABILITIES
                ),
                None,
            )
            if denied is not None:
                return self._error(
                    ctx,
                    ToolErrorCode.POLICY_DENIED,
                    f"{denied} is not available in a turn started by another "
                    "agent's message; the owner must request it as their own task",
                )
        if tool.precheck is not None:
            # Reject what no approval could make valid before asking for one.
            rejected = tool.precheck(arguments, ctx)
            if rejected is not None and not rejected.ok:
                error = rejected.error
                return self._error(
                    ctx,
                    error.code if error is not None else ToolErrorCode.UNKNOWN,
                    error.message if error is not None else "precheck rejected the call",
                    details=dict(error.details or {}) if error is not None else None,
                )
        for action in actions:
            scope = scope_from_context(ctx)
            if getattr(action, "cwd", None):
                with contextlib.suppress(Exception):  # the tool reports a bad cwd itself
                    scope = dataclasses.replace(
                        scope, command_cwd=ctx.sandbox.resolve(action.cwd, base=ctx.cwd)
                    )
            if tool_name.startswith("channel.") and ctx.channel_host is None:
                return self._error(ctx, ToolErrorCode.PERMISSION_DENIED, "No channel binding")
            self._event("PolicyChecked", {"tool": tool_name, "capability": action.capability})
            decision = self.policy.decide(
                action.capability,
                scope,
                path=action.fs_path,
                command=action.command,
                host=action.target if action.capability == CAPABILITY_NETWORK else None,
                risk=tool.risk,
                risk_class=tool.side_effects,
                # The approval of an image shows its connector and cost (#28).
                target=action.target if action.capability == "media.generate" else None,
                network_mode=getattr(action, "mode", None) or "read",
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

        sources = [s for s in (_external_source(a, tool) for a in actions) if s]
        if not sources or ctx.turn_state is None:
            return lambda: self._execute_authorized(tool, arguments, ctx, tool_call_id)

        def run_and_mark():
            try:
                return self._execute_authorized(tool, arguments, ctx, tool_call_id)
            finally:
                # Whatever came back (even an error page) is outside content.
                for source in sources:
                    ctx.turn_state.mark_external(source)

        return run_and_mark

    def _execute_authorized(self, tool, arguments, ctx, tool_call_id):
        tool_name = tool.name
        cancellation = ctx.cancellation
        if cancellation is not None:
            cancellation.throw_if_cancelled()

        call_ctx = dataclasses.replace(self._deadline_ctx(ctx, tool), tool_call_id=tool_call_id)
        attempts = 2 if tool.idempotent else 1
        attempt = 0
        while True:
            if call_ctx.deadline_at is not None and time.time() >= call_ctx.deadline_at:
                return self._error(ctx, ToolErrorCode.TIMEOUT, "Tool execution deadline exhausted")
            attempt += 1
            result = self._invoke_handler(tool, arguments, call_ctx, ctx, tool_call_id)
            if result.ok and tool_name.startswith("web.") and isinstance(result.data, dict):
                from rinari.tools.native.web import snapshot_id

                snapshot = next(
                    (v[1] for v in ctx.web_snapshots.values() if v[1].url == arguments.get("url")),
                    None,
                )
                if snapshot is not None and tool_name != "web.download":
                    result = dataclasses.replace(
                        result, data={**result.data, "source_id": snapshot_id(snapshot)}
                    )
            if result.ok and tool.output_schema:
                # P0.5: the contract is enforced, not decorative: dynamic
                # sources (plugins/MCP/OpenAPI) must honor their schema.
                result = self._check_output_schema(tool, tool_name, result, ctx)
            if (
                result.ok
                or attempt >= attempts
                or not self._retryable_result(tool, result, arguments)
            ):
                break
            if cancellation is not None:
                cancellation.throw_if_cancelled()
        return self._postprocess(result, ctx, tool_call_id, tool)

    def _check_output_schema(
        self, tool: ToolDefinition, tool_name: str, result: ToolResult, ctx: ToolContext
    ) -> ToolResult:
        """Validate a result against the tool's declared output schema.

        For MCP the schema describes `structuredContent` (the spec), not the
        whole payload. A result that breaks its schema but carries readable
        text still reaches the model, with a warning (plan 0.4.0 #48): the
        text was what mattered, and refusing it cost a retry. Only a result
        with nothing usable fails.
        """
        is_mcp = tool.manifest.get("source") == "mcp"
        data = result.data if isinstance(result.data, dict) else {}
        if is_mcp:
            subject = data.get("structuredContent")
            errors = (
                ["structuredContent is missing"]
                if subject is None
                else validate_against(tool.output_schema, subject)
            )
        else:
            errors = validate_against(tool.output_schema, result.data)
        if not errors:
            return result
        summary = "; ".join(errors[:5])
        texts = [
            item.get("text")
            for item in (data.get("content") or [])
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        ]
        if is_mcp and texts:
            return dataclasses.replace(
                result,
                data={**data, "schema_warning": f"the output does not match its schema: {summary}"},
            )
        return self._error(
            ctx,
            ToolErrorCode.VALIDATION_FAILED,
            f"tool {tool_name} output violates its output_schema: {summary}",
        )

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

    def _retryable_result(
        self, tool: ToolDefinition, result: ToolResult, arguments: dict | None = None
    ) -> bool:
        if not tool.idempotent:
            return False
        if tool.name == "http.request":
            # The HTTP adapter owns method-aware retries. Never multiply its
            # attempts or replay an ambiguous POST at this outer layer.
            return False
        if tool.side_effects != "none":
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
        deadline = time.time() + tool.timeout_ms / 1000.0
        if ctx.deadline_at is not None:
            deadline = min(deadline, ctx.deadline_at)
        return dataclasses.replace(ctx, deadline_at=deadline)

    def _invoke_handler(
        self,
        tool: ToolDefinition,
        arguments: dict,
        call_ctx: ToolContext,
        ctx: ToolContext,
        tool_call_id: str,
    ) -> ToolResult:
        observation = None
        from rinari.shared.execution_scope import execution_context, tool_output_context

        scope_token = execution_context.set(call_ctx)
        output_token = None
        if call_ctx.output_sink is not None:
            base_sink = call_ctx.output_sink
            stream_tails: dict[str, str] = {}
            secrets = tuple(getattr(self._redactor, "_secrets", ()))

            def bound_output_sink(stream: str, text: str) -> None:
                token = tool_output_context.set((tool.name, tool_call_id))
                try:
                    if not text:
                        return
                    combined = stream_tails.get(stream, "") + text
                    sanitized = self._redactor.redact(combined)
                    # Buffer only a possible secret prefix, so ordinary short
                    # progress lines stream immediately with credentials set.
                    keep = 0
                    for secret in secrets:
                        for length in range(min(len(secret) - 1, len(sanitized)), keep, -1):
                            if sanitized.endswith(secret[:length]):
                                keep = length
                                break
                    emit_text = sanitized[:-keep] if keep else sanitized
                    stream_tails[stream] = sanitized[-keep:] if keep else ""
                    if emit_text:
                        base_sink(stream, emit_text)
                finally:
                    tool_output_context.reset(token)

            call_ctx = dataclasses.replace(call_ctx, output_sink=bound_output_sink)
            output_token = tool_output_context.set((tool.name, tool_call_id))
        try:
            if call_ctx.change_tracker is not None:
                observation = call_ctx.change_tracker.before_tool(tool.name, arguments, call_ctx)
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
        except TimeoutError:
            return self._error(ctx, ToolErrorCode.TIMEOUT, "Tool execution deadline exhausted")
        except (ValueError, TypeError, KeyError) as exc:
            return self._error(
                ctx,
                ToolErrorCode.INVALID_ARGUMENT,
                f"Invalid tool arguments ({type(exc).__name__})",
            )
        except OSError as exc:
            return self._error(
                ctx,
                ToolErrorCode.NOT_FOUND,
                f"filesystem operation failed: {exc.__class__.__name__}",
            )
        except Exception as exc:
            return self._error(
                ctx,
                ToolErrorCode.UNKNOWN,
                f"Tool failed ({type(exc).__name__}); inspect engine diagnostics",
            )
        finally:
            if output_token is not None:
                for stream, tail in stream_tails.items():
                    if tail:
                        with contextlib.suppress(Exception):
                            base_sink(stream, self._redactor.redact(tail))
                tool_output_context.reset(output_token)
            execution_context.reset(scope_token)
            if call_ctx.change_tracker is not None:
                with contextlib.suppress(Exception):
                    call_ctx.change_tracker.after_tool(observation)

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
        scope_key = grant_scope_key(ctx)
        request = ApprovalRequest(
            capability=decision.capability,
            description=f"{tool.name}: {decision.reason}",
            target=decision.target,
            risk=decision.risk,
            session_id=ctx.session_id,
            project_id=scope_key,
            rule_id=decision.rule_id,
            reusable=decision.reusable,
            # A loose chat has no «always»: what is allowed in one chat does
            # not reach the others (#56).
            choices=(
                decision.choices
                if scope_key != LOOSE_CHATS
                else tuple(c for c in decision.choices if c != "allow_project")
            ),
            binding_mode=decision.binding_mode,
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
        if decision.capability == "fs.read" and decision.target and tool.side_effects == "none":
            target = Path(decision.target).resolve()
            sandbox = FilesystemSandbox(
                ctx.sandbox.read_root,
                ctx.sandbox.write_roots,
                unrestricted=ctx.sandbox.unrestricted,
                unrestricted_reads=ctx.sandbox.unrestricted_reads,
                approved_read_roots=(*ctx.sandbox.approved_read_roots, target),
            )
            return True, dataclasses.replace(ctx, sandbox=sandbox)
        # A granted write outside the base roots extends this call's sandbox
        # for exactly this action's target directory.
        if decision.capability == "fs.write" and decision.target:
            target = Path(decision.target)
            if target.is_absolute() and not self._inside_any(target, ctx.sandbox.write_roots):
                approved_root = target.parent
                sandbox = FilesystemSandbox(
                    ctx.sandbox.read_root,
                    (*ctx.sandbox.write_roots, approved_root),
                    unrestricted=ctx.sandbox.unrestricted,
                    unrestricted_reads=ctx.sandbox.unrestricted_reads,
                    approved_read_roots=(*ctx.sandbox.approved_read_roots, target),
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
        data = self._redact_payload(result.data)
        presentation = self._redact_payload(result.presentation)
        artifacts = result.artifacts
        if (
            isinstance(data, dict)
            and {"command", "argv", "stdout", "stderr", "exit_code"} & data.keys()
        ):
            presentation = {
                "kind": "command",
                "command": data.get("command") or data.get("argv"),
                "cwd": data.get("cwd"),
                "exit_code": data.get("exit_code"),
                "stdout": str(data.get("stdout") or "")[:64_000],
                "stderr": str(data.get("stderr") or "")[:64_000],
                "running": bool(data.get("running", False)),
                "truncated": bool(data.get("truncated", False)),
            }
        if isinstance(result.captured_output, dict):
            captured = self._redact_payload(result.captured_output)
            for stream in ("stdout", "stderr"):
                if captured.get(stream):
                    reference = self._spill(
                        f"{tool_call_id or 'tool'}-{stream}", captured[stream], ctx
                    )
                    artifacts = (*artifacts, reference)
            if isinstance(presentation, dict):
                presentation["capture_truncated"] = bool(captured.get("truncated"))
        sanitized = dataclasses.replace(
            result,
            data=data,
            presentation=presentation,
            captured_output=None,
            notes=tuple(self._redactor.redact(note) for note in result.notes),
            error=(
                dataclasses.replace(
                    result.error,
                    message=self._redactor.redact(result.error.message),
                    details=self._redact_payload(result.error.details),
                )
                if result.error
                else None
            ),
            artifacts=tuple(artifacts),
            truncated=result.truncated,
        )

        from rinari.tools.observations import omit_unreadable, project_result

        budget = self._result_budget(tool, data, ctx.observation_budget_bytes)
        try:
            sanitized = omit_unreadable(
                sanitized,
                spill=lambda suffix, content: self._spill(f"{tool_call_id}-{suffix}", content, ctx),
            )
            projected = project_result(
                sanitized,
                tool=tool.name if tool else "",
                budget=budget - 128,
                spill=lambda suffix, content, extension=None: self._spill(
                    f"{tool_call_id}-{suffix}", content, ctx, extension
                ),
            )
            return dataclasses.replace(projected, full_observation=sanitized)
        except OSError as exc:
            return dataclasses.replace(
                sanitized,
                ok=False,
                data=None,
                error=ToolErrorInfo(
                    ToolErrorCode.RESOURCE_EXHAUSTED,
                    f"Could not persist tool evidence ({type(exc).__name__}); "
                    "execution may have occurred, do not repeat it automatically",
                ),
            )

    def project_round(self, entries, ctx):
        from rinari.tools.observations import allocate_budgets, project_result

        sources = [result.full_observation or result for call, result in entries]
        # A result's demand is capped by its own inline limit, so a spilled
        # command never takes round budget it would not use.
        sizes = [
            min(
                len(source.to_model_text(call.name).encode("utf-8")) + 128,
                self._result_budget(
                    self.registry.get(call.name), source.data, ctx.observation_budget_bytes
                ),
            )
            for (call, _), source in zip(entries, sources, strict=True)
        ]
        budgets = allocate_budgets(sizes, ctx.observation_budget_bytes, ctx.round_observation_bytes)
        projected = []
        for (call, result), source, budget in zip(entries, sources, budgets, strict=True):
            output = project_result(
                source,
                tool=call.name,
                budget=max(0, budget - 128),
                spill=lambda suffix, content, extension=None, call=call: self._spill(
                    f"{call.id}-{suffix}", content, ctx, extension
                ),
            )
            projected.append(
                dataclasses.replace(
                    output,
                    duration_ms=result.duration_ms,
                    tool_call_id=call.id,
                    full_observation=None,
                )
            )
        if (
            sum(
                len(result.to_model_text(call.name).encode("utf-8"))
                for (call, _), result in zip(entries, projected, strict=True)
            )
            > ctx.round_observation_bytes
        ):
            raise ToolError(
                "Round observation budget cannot hold mandatory result metadata; "
                "increase runtime.tools.round_observation_bytes. "
                "Executed actions must not be repeated."
            )
        return projected

    def _result_budget(self, tool: ToolDefinition | None, data: Any, budget: int) -> int:
        """Bytes one observation may take inline before its excess is spilled.

        Process output (stdout/stderr) is capped by the configured artifact
        output threshold, a tool by its own max_output_bytes; the cap bounds
        the content and leaves room for the recovery metadata, so a capped
        result still fits with its artifact.read pointer.
        """
        from rinari.tools.definition import _is_process_output

        cap = tool.max_output_bytes if tool is not None and tool.max_output_bytes else None
        if _is_process_output(data):
            cap = min(cap or self.spill_threshold_bytes, self.spill_threshold_bytes)
        if cap is None:
            return budget
        return min(budget, cap + _RECOVERY_METADATA_BYTES)

    def _spill(
        self,
        tool_call_id: str,
        payload: str | bytes,
        ctx: ToolContext,
        extension: str | None = None,
    ) -> ArtifactRef:
        directory = ctx.artifact_root / ctx.session_id / "runtime"
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_call_id) or "result"
        safe_id = safe_id[:64]
        suffix = extension or (".bin" if isinstance(payload, bytes) else ".txt")
        digest = hashlib.sha256(
            payload if isinstance(payload, bytes) else payload.encode("utf-8")
        ).hexdigest()
        path = directory / f"{safe_id}-{digest}{suffix}"
        import os
        import tempfile

        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
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
            # Events are stored and shown: besides the known provider keys,
            # hide credentials recognizable by shape (a password typed into a
            # command). The call itself already ran with the real arguments.
            self._emit(type_, redact_value(payload, self._redactor.secrets))

    def redact_arguments(self, arguments):
        """Arguments as the stored record keeps them: known secrets and
        credential-shaped values hidden."""
        return redact_value(self._redact_payload(arguments), self._redactor.secrets)

    def _redact_payload(self, payload):
        if isinstance(payload, str):
            return self._redactor.redact(payload)
        if isinstance(payload, dict):
            return {k: self._redact_payload(v) for k, v in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self._redact_payload(v) for v in payload]
        return payload

    def _ctx_clock(self) -> Clock:
        return self._clock if self._clock is not None else SystemClock()

    def to_iso(self) -> str:
        import datetime

        return datetime.datetime.now(datetime.UTC).isoformat()
