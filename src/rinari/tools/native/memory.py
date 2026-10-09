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


# Without a project there is no project memory to read or write. Recall falls
# back to the owner's memory with a note (reading is harmless and usually what
# was wanted); writes refuse instead, because a project fact or convention
# filed as personal memory would follow the owner into every other chat.
_NO_PROJECT = (
    "this session has no project, so there is no project memory. Use scope=user "
    "for something about the owner, or open the folder as a project. Do not retry "
    "with scope=project."
)


def _no_project() -> ToolResult:
    return _fail(ToolErrorCode.INVALID_ARGUMENT, _NO_PROJECT)


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


def _owner_source(ctx: ToolContext) -> dict | None:
    """The owner message that started this turn, set by the host.

    Only turns the owner started (interactive or an owner channel) carry it:
    a subagent, a scheduled run or a peer message has none, so personal
    memory stays the owner's even when the model words the entry itself.
    """
    source = getattr(ctx, "memory_source", None)
    if (
        not isinstance(source, dict)
        or source.get("session_id") != ctx.session_id
        or not source.get("message_id")
    ):
        return None
    return source


def _remember_for_owner(
    service, input: dict, ctx: ToolContext, topic: str, text: str
) -> ToolResult:
    """Personal memory, written by the model in the owner's turn.

    The model distils what to keep in its own words; the record points at
    the owner message that started the turn. Sensitive personal data is not
    stored: it becomes a proposal the owner approves. Every refusal says
    whether retrying can help, because a code that suggested a pending
    approval made the model retry until the loop detector stopped the turn.
    """
    source = _owner_source(ctx)
    if source is None:
        return _fail(
            ToolErrorCode.PERMISSION_DENIED,
            "personal memory can only be written during a turn the owner started "
            "(not from a subagent, a scheduled run or another session). Do not retry.",
        )
    kind = input.get("kind", "fact")
    if kind not in _USER_KINDS:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"user kind must be one of: {', '.join(_USER_KINDS)}",
        )
    confidence = input.get("confidence", 1.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "confidence must be a number 0..1")
    try:
        result = service.remember_for_owner(
            ctx.session_id,
            source["message_id"],
            source.get("text") or "",
            text=text,
            topic=topic,
            kind=kind,
            confidence=float(confidence),
        )
    except Exception as exc:
        message = getattr(exc, "message", str(exc))
        return _fail(ToolErrorCode.VALIDATION_FAILED, f"{message} Do not retry with the same text.")
    if result.get("pending"):
        result["message"] = (
            "Sensitive personal data is not stored without the owner's approval: it was "
            "saved as a proposal for the owner to review. Tell the owner; do not retry."
        )
    return _ok(result)


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
            "memory is excluded for this conversation; the owner must re-enable it. Do not retry.",
        )
    if scope == "user":
        return _remember_for_owner(service, input, ctx, topic.strip(), text.strip())
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
            return _no_project()
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
    notes: tuple[str, ...] = ()
    if scope == "project" and _project_root(ctx) is None:
        scope = "user"
        notes = ("this session has no project: searched user memory instead of project memory",)
    try:
        if scope == "user":
            rows = service.search_user(query, kind=kind, limit=limit)
        elif scope == "project":
            root = _project_root(ctx)
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
    return ToolResult(
        ok=True, data={"scope": scope, "count": len(rows), "records": rows}, notes=notes
    )


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
    if scope == "user" and _owner_source(ctx) is None:
        return _fail(
            ToolErrorCode.PERMISSION_DENIED,
            "personal memory can only be updated during a turn the owner started. Do not retry.",
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
                return _no_project()
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
            f"{getattr(exc, 'message', str(exc))} Do not retry with the same values.",
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
                return _no_project()
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
                "fact about the owner or their environment that should survive "
                "across sessions, written in your own words (store only what the "
                "owner said or you verified — never speculation); only in a turn the "
                "owner started. Sensitive personal data (health, money, identity) is "
                "saved as a proposal for the owner to approve. "
                "scope=project: a stable fact/rule/convention of this project "
                "(only in a session with a project; a chat without one refuses it). "
                "scope=pattern: a reusable procedure. "
                "A re-statement with the same kind+topic refreshes the record; "
                "a conflicting statement supersedes the old one. Secrets are rejected. "
                "A refusal that says 'Do not retry' will fail again: tell the owner."
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
                "scope=project (this project's records; a chat without a project "
                "searches user memory instead and says so), scope=pattern (reusable "
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
