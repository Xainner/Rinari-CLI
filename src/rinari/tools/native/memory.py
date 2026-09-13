"""Memory tools (phase 4): explicit durable memory, never automatic.

The harness auto-persists nothing: a record only exists because the agent
explicitly stored it (user preference/decision, a verified project fact, an
episodic task summary, or a reusable pattern). Project memory requires a
PROJECT session; user memory works in both. Secret-looking text is rejected
by the service before storage.
"""

from __future__ import annotations

from pathlib import Path

from rinari.memory.service import MemoryConflictError, MemoryNotFoundError
from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_LOCAL_DESTRUCTIVE,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

_USER_KINDS = ["preference", "rule", "fact"]
_PROJECT_KINDS = ["fact", "rule", "convention"]
_SCOPES = ["user", "project", "pattern"]
_RECALL_SCOPES = ["user", "project", "pattern", "episodic"]


def _ok(data) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _service(ctx: ToolContext):
    return getattr(ctx, "memory", None)


def _project_root(ctx: ToolContext) -> Path | None:
    root = ctx.project_root if ctx.kind == "PROJECT" else None
    if root is None or not Path(root).is_dir():
        return None
    return Path(root).resolve()


def _classify_write(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.write", None)


def _classify_read(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.read", None)


def _optional_str(input: dict, key: str, *, max_len: int) -> tuple[str | None, ToolResult | None]:
    value = input.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, _fail(ToolErrorCode.INVALID_ARGUMENT, f"{key} must be a string")
    cleaned = value.strip()
    if len(cleaned) > max_len:
        return None, _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"{key} exceeds the maximum length of {max_len} characters",
        )
    return cleaned, None


def memory_remember(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "memory service unavailable")
    scope = input.get("scope")
    if scope not in _SCOPES:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, f"scope must be one of: {', '.join(_SCOPES)}")
    topic = input.get("topic")
    text = input.get("text")
    if not isinstance(topic, str) or not topic.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "topic is required")
    if len(topic.strip()) > 128:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "topic exceeds 128 characters")
    if not isinstance(text, str) or not text.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "text is required")
    if len(text.strip()) > 4096:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "text exceeds 4096 characters")
    if scope == "user" and not service.extraction_allowed(ctx.session_id):
        return _fail(
            ToolErrorCode.PERMISSION_DENIED,
            "memory is excluded for this conversation; owner must re-enable it explicitly",
        )
    if scope == "user":
        source = getattr(ctx, "memory_source", None)
        if not isinstance(source, dict) or source.get("session_id") != ctx.session_id:
            return _fail(
                ToolErrorCode.PERMISSION_DENIED,
                "user memory writes require a validated owner-message source",
            )
        if source.get("text") != text.strip() or not source.get("message_id"):
            return _fail(
                ToolErrorCode.APPROVAL_REQUIRED,
                "user memory must quote the validated owner message exactly",
            )
        candidate = service.capture_owner_message(
            ctx.session_id, source["message_id"], source["text"]
        )
        if candidate is None:
            return _fail(
                ToolErrorCode.PERMISSION_DENIED,
                "the owner-message source is unavailable or has been withdrawn",
            )
        if candidate.get("status") != "accepted":
            return _ok({"pending": True, "candidate": candidate})
        return _ok({"id": candidate["memory_id"], "candidate": candidate})
    provenance = input.get("provenance")
    if provenance is not None and not isinstance(provenance, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "provenance must be a string")
    confidence = input.get("confidence", 1.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "confidence must be a number 0..1")

    if scope == "user":
        kind = input.get("kind", "preference")
        if kind not in _USER_KINDS:
            return _fail(
                ToolErrorCode.INVALID_ARGUMENT,
                f"user kind must be one of: {', '.join(_USER_KINDS)}",
            )
    elif scope == "project":
        root = _project_root(ctx)
        if root is None:
            return _fail(
                ToolErrorCode.DEPENDENCY_ERROR,
                "project memory requires a PROJECT session with a project root",
            )
        kind = input.get("kind", "fact")
        if kind not in _PROJECT_KINDS:
            return _fail(
                ToolErrorCode.INVALID_ARGUMENT,
                f"project kind must be one of: {', '.join(_PROJECT_KINDS)}",
            )
    else:
        kind = ""

    try:
        if scope == "user":
            result = service.remember_user(
                text,
                kind=kind,
                topic=topic,
                provenance=provenance or "agent",
                confidence=float(confidence),
            )
        elif scope == "project":
            result = service.remember_project(
                str(root),
                text,
                kind=kind,
                topic=topic,
                provenance=provenance or "agent",
                confidence=float(confidence),
            )
        else:
            p_scope = input.get("pattern_scope", "global")
            if p_scope not in ("global", "user"):
                return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern_scope must be global or user")
            result = service.remember_pattern(
                topic, text, scope=p_scope, provenance=provenance or "agent"
            )
    except Exception as exc:
        return _fail(ToolErrorCode.VALIDATION_FAILED, getattr(exc, "message", str(exc)))
    return _ok(result)


def memory_recall(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "memory service unavailable")
    scope = input.get("scope")
    if scope not in _RECALL_SCOPES:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"scope must be one of: {', '.join(_RECALL_SCOPES)}",
        )
    query = input.get("query", "")
    if not isinstance(query, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "query must be a string")
    kind = input.get("kind")
    if kind is not None and not isinstance(kind, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "kind must be a string")
    limit = input.get("limit", 10)
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "limit must be an integer 1..50")
    try:
        if scope == "user":
            rows = service.search_user(query, kind=kind, limit=limit)
        elif scope == "project":
            root = _project_root(ctx)
            if root is None:
                return _fail(
                    ToolErrorCode.DEPENDENCY_ERROR,
                    "project memory requires a PROJECT session with a project root",
                )
            rows = service.search_project(str(root), query, kind=kind, limit=limit)
        elif scope == "pattern":
            p_scope = input.get("pattern_scope")
            if p_scope is not None and not isinstance(p_scope, str):
                return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern_scope must be a string")
            rows = service.search_pattern(query, scope=p_scope, limit=limit)
        else:
            root = _project_root(ctx)
            rows = service.search_episodic(str(root) if root else "", query, limit=limit)
    except Exception as exc:
        return _fail(ToolErrorCode.UNKNOWN, getattr(exc, "message", str(exc)))
    return _ok({"scope": scope, "count": len(rows), "records": rows})


def memory_update(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "memory service unavailable")
    scope = input.get("scope")
    memory_id = input.get("id")
    if scope not in ("user", "project"):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "scope must be user or project")
    if not isinstance(memory_id, str) or not memory_id:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "id is required")
    if scope == "user":
        source = getattr(ctx, "memory_source", None)
        if not isinstance(source, dict) or source.get("session_id") != ctx.session_id:
            return _fail(
                ToolErrorCode.PERMISSION_DENIED,
                "user memory updates require a validated owner-message source",
            )
    expected_revision = input.get("expected_revision")
    if scope == "user" and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            "expected_revision is required for user memory updates",
        )
    text, err = _optional_str(input, "text", max_len=4096)
    if err is not None:
        return err
    if scope == "user":
        source = getattr(ctx, "memory_source", None)
        if text is None or source.get("text") != text:
            return _fail(
                ToolErrorCode.APPROVAL_REQUIRED,
                "user memory updates must quote the validated owner message exactly",
            )
    topic, err = _optional_str(input, "topic", max_len=128)
    if err is not None:
        return err
    confidence = input.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "confidence must be a number 0..1")
    provenance, err = _optional_str(input, "provenance", max_len=256)
    if err is not None:
        return err
    try:
        if scope == "user":
            row = service.update_user(
                memory_id,
                text=text,
                topic=topic,
                confidence=confidence,
                provenance=provenance,
                expected_version=input.get("expected_version"),
                expected_revision=expected_revision,
            )
        else:
            root = _project_root(ctx)
            if root is None:
                return _fail(
                    ToolErrorCode.DEPENDENCY_ERROR,
                    "project memory requires a PROJECT session with a project root",
                )
            row = service.update_project(
                str(root),
                memory_id,
                text=text,
                topic=topic,
                confidence=confidence,
                provenance=provenance,
                expected_version=input.get("expected_version"),
            )
    except MemoryConflictError as exc:
        return _fail(ToolErrorCode.CONFLICT, exc.message)
    except MemoryNotFoundError as exc:
        return _fail(ToolErrorCode.NOT_FOUND, exc.message)
    except Exception as exc:
        return _fail(
            ToolErrorCode.VALIDATION_FAILED,
            getattr(exc, "message", str(exc)),
        )
    return _ok(row)


def memory_forget(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "memory service unavailable")
    scope = input.get("scope")
    memory_id = input.get("id")
    if scope not in ("user", "project", "pattern"):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "scope must be user, project, or pattern")
    if not isinstance(memory_id, str) or not memory_id:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "id is required")
    expected_revision = input.get("expected_revision")
    if scope == "user" and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            "expected_revision is required for user memory deletion",
        )
    try:
        if scope == "user":
            forgotten = service.forget_user(memory_id, expected_revision=expected_revision)
        elif scope == "pattern":
            forgotten = service.forget_pattern(memory_id)
        else:
            root = _project_root(ctx)
            if root is None:
                return _fail(
                    ToolErrorCode.DEPENDENCY_ERROR,
                    "project memory requires a PROJECT session with a project root",
                )
            forgotten = service.forget_project(str(root), memory_id)
    except MemoryConflictError as exc:
        return _fail(ToolErrorCode.CONFLICT, exc.message)
    except Exception as exc:
        return _fail(ToolErrorCode.VALIDATION_FAILED, getattr(exc, "message", str(exc)))
    return _ok({"scope": scope, "id": memory_id, "forgotten": forgotten})


def memory_episodic(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "memory service unavailable")
    summary = input.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "summary is required (max 400 chars)")
    outcome = input.get("outcome", "")
    if not isinstance(outcome, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "outcome must be a string")
    provenance = input.get("provenance")
    if provenance is not None and not isinstance(provenance, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "provenance must be a string")
    root = _project_root(ctx)
    try:
        record = service.record_episodic(
            ctx.session_id,
            str(root) if root else "",
            summary,
            outcome=outcome,
            provenance=provenance or "agent",
        )
    except Exception as exc:
        return _fail(ToolErrorCode.VALIDATION_FAILED, getattr(exc, "message", str(exc)))
    return _ok(record)


def memory_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="memory.remember",
            description=(
                "Store one explicit durable memory. scope=user: a preference/rule/"
                "fact that should survive across sessions (store only what the user "
                "actually stated or verified — never your own speculation). "
                "scope=project: a stable fact/rule/convention of this project "
                "(PROJECT sessions only). scope=pattern: a reusable procedure. "
                "A re-statement with the same topic kind+topic refreshes the record; "
                "a conflicting statement supersedes the old one. Secrets are rejected."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": _SCOPES},
                    "kind": {
                        "type": "string",
                        "enum": [*_USER_KINDS, *_PROJECT_KINDS],
                    },
                    "pattern_scope": {"type": "string", "enum": ["global", "user"]},
                    "topic": {"type": "string"},
                    "text": {"type": "string"},
                    "provenance": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["scope", "topic", "text"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=True,
            timeout_ms=15_000,
            handler=memory_remember,
            classify=_classify_write,
            namespace="memory",
        ),
        ToolDefinition(
            name="memory.recall",
            description=(
                "Search durable memory. scope=user (cross-session preferences/facts), "
                "scope=project (this project's records), scope=pattern (reusable "
                "procedures), scope=episodic (summarized past tasks). Records are "
                "possibly stale: re-verify volatile facts before relying on them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": _RECALL_SCOPES},
                    "query": {"type": "string"},
                    "kind": {"type": "string"},
                    "pattern_scope": {"type": "string", "enum": ["global", "user"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["scope"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=15_000,
            handler=memory_recall,
            classify=_classify_read,
            namespace="memory",
        ),
        ToolDefinition(
            name="memory.update",
            description=(
                "Update an existing user or project memory record by id (text, "
                "topic, confidence, provenance). Use after a stored fact turns "
                "out to be wrong or outdated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "project"]},
                    "id": {"type": "string"},
                    "expected_version": {
                        "type": "string",
                        "description": "updated_at from recall; reject a concurrent update",
                    },
                    "expected_revision": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "revision from recall; required for user memory",
                    },
                    "text": {"type": "string"},
                    "topic": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "provenance": {"type": "string"},
                },
                "required": ["scope", "id"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=15_000,
            handler=memory_update,
            classify=_classify_write,
            namespace="memory",
        ),
        ToolDefinition(
            name="memory.forget",
            description=(
                "Delete one user, project, or pattern memory record by id. Use when "
                "the user asks you to forget something or a stored record is wrong."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "project", "pattern"]},
                    "id": {"type": "string"},
                    "expected_revision": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "revision from recall; required for user memory",
                    },
                },
                "required": ["scope", "id"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_DESTRUCTIVE,
            idempotent=True,
            timeout_ms=15_000,
            handler=memory_forget,
            classify=_classify_write,
            namespace="memory",
        ),
        ToolDefinition(
            name="memory.episodic",
            description=(
                "Record a short episodic summary of a finished task for this "
                "session/project: what was done and the outcome. Max 400 chars. "
                "Call it when a meaningful task completes, not after every turn."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": 400},
                    "outcome": {"type": "string"},
                    "provenance": {"type": "string"},
                },
                "required": ["summary"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            timeout_ms=15_000,
            handler=memory_episodic,
            classify=_classify_write,
            namespace="memory",
        ),
    ]
