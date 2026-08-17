"""Tool contract: definition, result envelope, and context (tools.md 60-62).

`ToolDefinition` is the canonical metadata every tool source (native,
plugin, MCP, OpenAPI) normalizes into. `ToolResult` is the common
envelope; error codes follow the standard error classes in tools.md 62.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from rinari.models.types import ToolSchema
from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import Clock

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"

SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_LOCAL_REVERSIBLE = "local-reversible"
SIDE_EFFECT_LOCAL_DESTRUCTIVE = "local-destructive"
SIDE_EFFECT_REMOTE_REVERSIBLE = "remote-reversible"
SIDE_EFFECT_REMOTE_DESTRUCTIVE = "remote-destructive"
SIDE_EFFECT_COMMUNICATION = "communication"
SIDE_EFFECT_FINANCIAL = "financial"
SIDE_EFFECT_CREDENTIAL = "credential"


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    CONFLICT = "CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    CANCELLED = "CANCELLED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ToolErrorInfo:
    code: ToolErrorCode
    message: str
    retryable: bool = False
    details: Any = None


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    uri: str
    name: str
    kind: str = "file"


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: ToolErrorInfo | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    tool_call_id: str = ""
    duration_ms: float = 0.0
    timestamp: str = ""
    side_effects: tuple[str, ...] = ()
    truncated: bool = False
    origin: str = "native"

    def to_model_text(self) -> str:
        """Bounded, model-visible rendering of the result."""
        import json

        try:
            payload: Any = self.data if self.ok else (self.error.message if self.error else None)
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(self.data)
        if len(text) <= len(self.truncation_mark()) + 2048:
            return text
        return text[:2048] + self.truncation_mark()

    @staticmethod
    def truncation_mark() -> str:
        return "\n[output truncated]"


class ClassifiedAction:
    """What the action touches, extracted from the tool input."""

    def __init__(self, capability: str, target: str | None = None) -> None:
        self.capability = capability
        self.target = target

    @property
    def fs_path(self) -> str | None:
        return self.target if self.capability.startswith("fs.") else None

    @property
    def command(self) -> str | None:
        return self.target if self.capability == "shell.exec" else None


# Live output sink: (stream_name, text_chunk) as bytes arrive from a process.
# Provided by the session host (REPL) for live display; None for one-shot/JSON.
OutputSink = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class ToolContext:
    session_id: str
    kind: str
    cwd: Path
    project_root: Path | None
    user_home: Path | None
    profile: PermissionProfile
    sandbox: FilesystemSandbox
    limits: ProcessLimits
    artifact_root: Path
    clock: Clock
    cancellation: Any = None
    environment: dict[str, str] | None = None
    output_sink: OutputSink | None = None
    # Mutable session-scoped process registry for process.* tools (None = disabled).
    processes: Any = None
    # Session-scoped PtyRegistry (tools.native.pty); None on platforms without
    # a POSIX pty or for CHAT sessions that disable interactive processes.
    pty: Any = None
    # WorktreeGuard (projects.worktree) with the session's dirty-tree
    # baseline; None for CHAT sessions or repos without dirty state.
    worktree: Any = None
    # Session-scoped LspManager (rinari.lsp); None for CHAT sessions or when
    # no language server is registered/available.
    lsp: Any = None
    # Application-level VerificationService (rinari.verify); None only when the
    # session is not wired to the full service container.
    validation: Any = None
    # Application-level MemoryService (rinari.memory); None only when the
    # session is not wired to the full service container.
    memory: Any = None
    # Application-level ContextRetrievalService (rinari.context.retrieval);
    # None only when the session is not wired to the full service container.
    context_retrieval: Any = None
    # Session trust snapshot for the project (drives which project-supplied
    # data the agent may consume, e.g. in verification plans).
    project_trusted: bool = True
    # NetworkGuard (policy.network): network-capable tools must pass every
    # connection target through guard.assert_reachable before dialing.
    network: Any = None
    # Zero-arg factory returning an httpx-compatible client for web.* tools
    # (test seam: httpx.MockTransport); None -> fresh real client per request.
    web: Any = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    risk: str = RISK_LOW
    side_effects: str = SIDE_EFFECT_NONE
    idempotent: bool = True
    timeout_ms: int = 30_000
    max_output_bytes: int | None = None
    handler: Callable[[dict, ToolContext], ToolResult] | None = None
    classify: Callable[[dict], ClassifiedAction] | None = None
    always_loaded: bool = True
    namespace: str = "core"
    manifest: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.input_schema,
        )

    def classify_action(self, input: dict[str, Any]) -> ClassifiedAction:
        if self.classify is not None:
            return self.classify(input)
        if self.name.startswith("fs.read") or self.name in (
            "fs.list",
            "fs.glob",
            "fs.search_text",
            "fs.stat",
            "fs.diff",
            "fs.exists",
        ):
            return ClassifiedAction("fs.read", str(input.get("path") or ""))
        if self.name in ("fs.write", "fs.patch"):
            return ClassifiedAction("fs.write", str(input.get("path") or ""))
        if self.name == "shell.exec":
            return ClassifiedAction("shell.exec", str(input.get("command") or ""))
        if self.name.startswith("git."):
            return ClassifiedAction("git.local", self.project_target(input))
        if "network.outbound" in self.capabilities or self.namespace in ("web", "http", "browser"):
            target = input.get("url") or input.get("href") or input.get("host") or ""
            return ClassifiedAction("network.outbound", str(target))
        return ClassifiedAction(self.name)

    @staticmethod
    def project_target(input: dict[str, Any]) -> str | None:
        return str(input.get("path")) if input.get("path") is not None else None
