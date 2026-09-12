"""Normalized model runtime types (harness.md sections 28, 81, 82).

Every provider family normalizes into these types so session state stays
provider-independent: none of these values carry provider-specific thread or
response identifiers (harness.md section 28). Those may be cached elsewhere
as metadata at most.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

STOP_END_TURN = "end_turn"
STOP_TOOL_CALLS = "tool_calls"
STOP_MAX_TOKENS = "max_tokens"


class StopReason(StrEnum):
    END_TURN = STOP_END_TURN
    TOOL_CALLS = STOP_TOOL_CALLS
    MAX_TOKENS = STOP_MAX_TOKENS


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model-requested tool invocation. Arguments are already parsed."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Set when the wire arguments were not valid JSON (or not an object):
    # arguments stays {} and the tool must NOT execute.
    raw_arguments: str | None = None
    arguments_invalid: bool = False


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    images: tuple[Any, ...] = ()
    attachments: tuple[dict[str, Any], ...] = ()
    display_content: str | None = None

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role=ROLE_SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role=ROLE_USER, content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: tuple[ToolCall, ...] = ()) -> ChatMessage:
        return cls(role=ROLE_ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> ChatMessage:
        return cls(role=ROLE_TOOL, content=content, tool_call_id=tool_call_id, name=name)


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalized token usage. Unavailable metrics stay None, never 0-guessed."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Provenance of the numbers (§6.2): "complete" (authoritative payload),
    # "partial" (accumulated deltas), "unavailable" (endpoint sent nothing).
    source: str = "complete"

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelItem:
    """One provider response item (P0.6/Etapa D).

    Typed blocks (text/function_call/tool_use/...) carry parsed data;
    anything else is preserved opaque in ``data`` so protocol-required
    blocks (e.g. Anthropic thinking tied to tool use) survive round-trips.
    """

    type: str
    id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()
    stop_reason: StopReason = StopReason.END_TURN
    raw: dict[str, Any] | None = None
    items: tuple[ModelItem, ...] = ()
    provider_state: dict[str, Any] | None = None
    """Transport metadata (e.g. response id for chained calls). Never the
    source of truth: Rinari session history stays canonical."""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool = False
    tool_calls: bool = False
    structured_output: bool = False
    max_context_tokens: int | None = None
    reasoning_effort: bool = False
    vision: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Provider-agnostic tool declaration handed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    json_response: bool = False
    session_id: str | None = None
    """Opaque conversation id, forwarded only to vendors that require
    session affinity (e.g. OpenCode's x-opencode-session header)."""
