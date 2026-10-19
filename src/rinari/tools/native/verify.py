"""Verification tools: record evidence, plan verification, evaluate the gate.

The completion gate is a harness decision, not a model claim: the agent
records raw evidence (`verify.record`), the planner turns changed files into
concrete checks (`verify.plan`), and the gate folds the latest evidence into
a completion outcome (`verify.evaluate`). The harness re-evaluates the gate
after every turn with tool activity (finalize transition), so a "fixed"
claim without passing recorded evidence cannot become DONE.
"""

from __future__ import annotations

from pathlib import Path

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)


def _ok(data) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _service(ctx: ToolContext):
    return ctx.validation if getattr(ctx, "validation", None) is not None else None


def _root(ctx: ToolContext) -> Path | None:
    root = ctx.project_root if ctx.project_root is not None else ctx.cwd
    if root is None or not Path(root).is_dir():
        return None
    return Path(root).resolve()


def _classify_state_write(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.write", None)


def _classify_state_read(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.read", None)


def _as_str_list(value, *, max_items: int = 200) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    items = [str(v) for v in value if str(v).strip()]
    return items[:max_items]


def verify_record(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    root = _root(ctx)
    if service is None or root is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "verification service unavailable")
    kind = input.get("kind")
    result = input.get("result")
    if not isinstance(kind, str) or not isinstance(result, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "kind and result are required strings")
    try:
        record = service.record(
            root,
            kind=kind,
            result=result,
            command=str(input.get("command") or ""),
            summary=str(input.get("summary") or ""),
            detail=str(input.get("detail") or ""),
            artifact_ref=input.get("artifact_ref")
            if isinstance(input.get("artifact_ref"), str)
            else None,
            session_ref=ctx.session_id,
        )
    except Exception as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, getattr(exc, "message", str(exc)))
    data = {
        "id": record["id"],
        "kind": record["kind"],
        "result": record["result"],
        "summary": record["summary"],
        "created_at": record["created_at"],
    }
    return _ok(data)


def verify_plan(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    root = _root(ctx)
    if service is None or root is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "verification service unavailable")
    changed = _as_str_list(input.get("changed_files"))
    constraints = _as_str_list(input.get("user_constraints"))
    if changed is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "changed_files must be an array of paths")
    if constraints is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "user_constraints must be an array of strings")
    try:
        plan = service.plan(
            root,
            changed,
            user_constraints=constraints,
            trusted=ctx.project_trusted,
        )
    except Exception as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, getattr(exc, "message", str(exc)))
    return _ok(plan.to_dict())


def verify_evaluate(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    root = _root(ctx)
    if service is None or root is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "verification service unavailable")
    kinds = _as_str_list(input.get("required_kinds"), max_items=10)
    unresolved = _as_str_list(input.get("unresolved"), max_items=50)
    task_ids = _as_str_list(input.get("task_ids"), max_items=50)
    record_ids = _as_str_list(input.get("record_ids"), max_items=50)
    if kinds is None or unresolved is None or task_ids is None or record_ids is None:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            "required_kinds, unresolved, task_ids and record_ids must be arrays",
        )
    # Ids verify.record just returned, passed as task_ids, were refused as
    # "Task not found" (plan 0.4.0 #61c): they are evidence ids.
    from rinari.verify.records import VALIDATION_ID_PREFIX

    evidence_ids = [i for i in task_ids if i.startswith(f"{VALIDATION_ID_PREFIX}_")]
    task_ids = [i for i in task_ids if i not in evidence_ids]
    record_ids = [*record_ids, *evidence_ids]
    # Ids of this conversation's checklist, passed as task_ids, were refused
    # again and again (F27 bis): they are checklist items, not tasks. An item
    # not completed yet is still open work, so it counts as unresolved.
    checklist_items = _checklist_items(ctx, task_ids)
    if checklist_items:
        task_ids = [i for i in task_ids if i not in checklist_items]
        unresolved = [
            *unresolved,
            *(
                f"checklist item {item_id} ({item.get('status')}): {item.get('content')}"
                for item_id, item in checklist_items.items()
                if item.get("status") != "completed"
            ),
        ]
    if not kinds:
        # What verify.plan said: with no test suite, the evidence is checking
        # the result by hand (manual/custom), not a test the project lacks.
        kinds = ["test"] if service.has_test_suite(root) else []
    blocked_reason = input.get("blocked_reason")
    require_evidence = input.get("require_evidence")
    if not isinstance(blocked_reason, str) and blocked_reason is not None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "blocked_reason must be a string")
    if not isinstance(require_evidence, bool) and require_evidence is not None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "require_evidence must be a boolean")
    try:
        decision = service.evaluate(
            root,
            required_kinds=tuple(kinds),
            unresolved=unresolved,
            blocked_reason=blocked_reason,
            task_ids=task_ids,
            require_evidence=True if require_evidence is None else require_evidence,
            record_ids=set(record_ids) if record_ids else None,
        )
    except Exception as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, getattr(exc, "message", str(exc)))
    result = decision.to_dict()
    if checklist_items:
        result["checklist_ids"] = sorted(checklist_items)
        result["note"] = (
            "These ids are checklist items, not tasks (task_…); they were read from the "
            "checklist and any item not completed counts as unresolved."
        )
    return _ok(result)


def _checklist_items(ctx: ToolContext, ids: list[str]) -> dict[str, dict]:
    """Items of this conversation's checklist named by `ids` (task ids excluded)."""
    service = getattr(ctx, "checklist", None)
    session_id = getattr(ctx, "session_id", None)
    candidates = [i for i in ids if not i.startswith("task_")]
    if service is None or not session_id or not candidates:
        return {}
    try:
        current = service.get(session_id)
    except Exception:
        return {}
    if current is None:
        return {}
    by_id = {str(item.get("id")): item for item in current.items}
    return {i: by_id[i] for i in candidates if i in by_id}


def verify_tools() -> list[ToolDefinition]:
    kind_values = ["test", "lint", "typecheck", "build", "schema", "manual", "custom"]
    result_values = ["passed", "failed", "error", "skipped"]
    return [
        ToolDefinition(
            name="verify.record",
            description=(
                "Record one unit of validation evidence for this project. Run the "
                "commands you planned, then record each outcome here (kind, result, "
                "and the meaningful tail of the output as detail) before declaring "
                "work complete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": kind_values},
                    "result": {"type": "string", "enum": result_values},
                    "command": {"type": "string"},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                    "artifact_ref": {"type": "string"},
                },
                "required": ["kind", "result"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=30_000,
            handler=verify_record,
            classify=_classify_state_write,
            namespace="verify",
        ),
        ToolDefinition(
            name="verify.plan",
            description=(
                "Plan what to verify for a set of changed files: targeted tests "
                "(from the repository index or conventions), adjacent tests, whether "
                "the broader suite must run, discovered lint/typecheck/build "
                "commands, and a risk level."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "user_constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["changed_files"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=verify_plan,
            classify=_classify_state_read,
            namespace="verify",
        ),
        ToolDefinition(
            name="verify.evaluate",
            description=(
                "Evaluate the completion gate from the latest recorded evidence. "
                "Returns outcome DONE | IMPLEMENTED_UNVERIFIED | PARTIAL | BLOCKED | "
                "FAILED plus reasons and the evidence used. Pass unresolved failures "
                "you are aware of; a claimed success with failing recorded evidence "
                "evaluates to FAILED. Without required_kinds, a project with a test "
                "suite needs a test and one without needs any passing check (manual "
                "counts). record_ids limits it to the evidence verify.record returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "required_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "unresolved": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "blocked_reason": {"type": "string"},
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "record_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "require_evidence": {"type": "boolean"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=30_000,
            handler=verify_evaluate,
            classify=_classify_state_read,
            namespace="verify",
        ),
    ]
