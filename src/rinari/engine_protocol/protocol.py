"""Engine Protocol constants (Rinari Code Phase 1, engine side)."""

from __future__ import annotations

PROTOCOL_NAME = "rinari-engine"
PROTOCOL_VERSION = 1


def engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("rinari")
    except Exception:
        return "0.1.0"


CAPABILITIES: dict[str, bool] = {
    "tool_contracts_v1": True,
    "durable_operations_v1": True,
    "channel_tools_v1": True,
    "image_attachments_v1": True,
    "document_attachments_v1": True,
    "ocr_attachments_v1": True,
    "structured_tool_activity_v1": True,
    "ssh_targets_v1": True,
    "plan_read_scope_v1": True,
    "web_preview_v1": True,
    "desktop_workspace_v1": True,
    "interactive_questions_v1": True,
    "chat": True,
    "projects": True,
    "browser": True,
    "mcp": True,
    "plugins": True,
    "subagents": True,
    "artifacts": True,
    "checkpoints": True,
    "terminal": True,
    # Session permissions, attachment/file search, model/tool lifecycle
    # telemetry, approval expiry and runtime snapshot recovery.
    "desktop_turn_runtime_v3": True,
    # Correlated, persisted, replayable narrative activity for Rinari Code.
    "activity_timeline_v1": True,
    "permission_profiles_v2": True,
    "turn_changeset_v1": True,
    "personal_memory_v1": True,
}
