"""Lifecycle hooks (phase 5).

Decision record (2026-08-17, see TODO.md "Registro de decisiones"):

- Declarations live in `hooks.json` files: user scope in
  `~/.rinari/hooks.json`, project scope in `<root>/.rinari/hooks.json`
  (requires project trust), plus plugin-contributed hooks. Enable/disable
  state is persisted in the `hooks` table per (scope, source, name).
- Handler types: `python` (dotted `module:function`) and `shell` (command;
  the payload is delivered as JSON on stdin, output is captured). Per
  harness.md 107 hooks do **not** execute arbitrary shell implicitly: a
  shell handler must declare the `shell.exec` capability, and project-scope
  hooks only execute when the project is trusted.
- Deterministic order: (source rank, name). A failing hook never takes the
  session down: outcomes are recorded and returned.
- v1 wiring: SessionStart/SessionEnd (session flow), BeforeModel/AfterModel,
  PreToolUse/PostToolUse/ToolError, BeforeFinal (agent loop),
  BeforeCompact/AfterCompact (pressure hook), PermissionRequest (approval
  engine). SubagentStart/SubagentStop fire from the orchestrator (phase 6).
"""

from rinari.hooks.engine import HookDeclaration, HookEngine, HookOutcome
from rinari.hooks.events import (
    ALL_EVENTS,
    EVENT_AFTER_COMPACT,
    EVENT_AFTER_MODEL,
    EVENT_BEFORE_COMPACT,
    EVENT_BEFORE_FINAL,
    EVENT_BEFORE_MODEL,
    EVENT_PERMISSION_REQUEST,
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_SUBAGENT_START,
    EVENT_SUBAGENT_STOP,
    EVENT_TOOL_ERROR,
)
from rinari.hooks.service import HookService

__all__ = [
    "ALL_EVENTS",
    "EVENT_AFTER_COMPACT",
    "EVENT_AFTER_MODEL",
    "EVENT_BEFORE_COMPACT",
    "EVENT_BEFORE_FINAL",
    "EVENT_BEFORE_MODEL",
    "EVENT_PERMISSION_REQUEST",
    "EVENT_POST_TOOL_USE",
    "EVENT_PRE_TOOL_USE",
    "EVENT_SESSION_END",
    "EVENT_SESSION_START",
    "EVENT_SUBAGENT_START",
    "EVENT_SUBAGENT_STOP",
    "EVENT_TOOL_ERROR",
    "HookDeclaration",
    "HookEngine",
    "HookOutcome",
    "HookService",
]
