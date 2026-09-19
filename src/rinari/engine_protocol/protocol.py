"""Engine Protocol constants (Rinari Code Phase 1, engine side)."""

from __future__ import annotations

import hashlib

PROTOCOL_NAME = "rinari-engine"
PROTOCOL_VERSION = 1


def engine_version() -> str:
    try:
        from importlib.metadata import version

        return version("rinari")
    except Exception:
        return "0.1.0"


CAPABILITIES: dict[str, bool] = {
    "persistent_context_compaction_v1": True,
    "local_image_view_v1": True,
    "vision_routing_v1": True,
    "vision_routing_v2": True,
    "vision_message_routing_v3": True,
    "model_execution_policy_v1": True,
    "durable_turn_recovery_v1": True,
    "desktop_processes_v1": True,
    # Strong process identity: engine_instance_id per boot plus a
    # per-resource generation token validated by workspace.process.stop.
    "process_identity_v1": True,
    "agent_inheritance_v1": True,
    "agent_activity_v1": True,
    "browser_view_v1": True,
    "tool_contracts_v1": True,
    "durable_operations_v1": True,
    "channel_tools_v1": True,
    "image_attachments_v1": True,
    "document_attachments_v1": True,
    "ocr_attachments_v1": True,
    "structured_tool_activity_v1": True,
    "recoverable_tool_results_v1": True,
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
    "selective_memory_v1": True,
    "memory_privacy_v1": True,
    "personal_memory_controls_v1": True,
    # Peer messaging between agent sessions (Boards): untrusted data, per-target
    # consent, provenance ceiling in the receiving turn.
    "session_peer_messaging_v1": True,
    "project_flow_v1": True,
}


def home_id(home) -> str:
    """Stable identity of an Engine home: a digest of its resolved path.

    It changes only when the home moves; `engine_instance_id` changes on every
    start. Clients use it to namespace presentation state (layouts, anchors)
    so two homes never share it. Not a secret and not reversible in practice.
    """
    from pathlib import Path

    resolved = str(Path(home).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
