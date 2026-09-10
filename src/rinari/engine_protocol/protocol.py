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
}
