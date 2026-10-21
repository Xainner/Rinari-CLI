"""Centralized prompt assembly (harness.md sections 33-37).

Stable ordering by authority, explicit trust per segment, untrusted
content wrapped as data. The assembler is pure: the session supplies the
`AssemblerContext`, the assembler decides the shape. No segment is built
here beyond what the context provides — loading Soul/Constitution, policy
snapshots, and context retrieval happen upstream.

Two placements keep the provider's prompt cache useful. Segments that only
change with the session's configuration (constitution, policy, Soul,
instructions, skills, compact state, stable environment facts) form the
system prompt, the cached prefix. Segments that change as the work
progresses (`CachePolicy.TURN`: task graph, query-ranked memory, the
repository scan, on-demand identity, evidence) form `turn_context`, sent
after the history on every request and never stored: a change there costs
only the note itself, not the whole conversation behind it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from rinari.models.types import TURN_CONTEXT_SOURCE, ChatMessage
from rinari.prompts.language import language_note, owner_language
from rinari.prompts.segments import (
    CachePolicy,
    PromptSegment,
    SegmentKind,
    SegmentTrust,
)


@dataclass(frozen=True, slots=True)
class ProjectInstruction:
    provenance: str
    content: str


@dataclass(frozen=True, slots=True)
class ActiveSkill:
    name: str
    summary: str


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    source: str
    content: str


@dataclass(frozen=True, slots=True)
class AssemblerContext:
    session_kind: str = "CHAT"
    constitution: str = ""
    runtime_policy: str = ""
    # Canonical Soul only (harness.md 37). The Extended Identity Reference
    # lives in `extended_identity` and is injected only per-turn when
    # `include_extended_identity` is set by the session host.
    soul: str = ""
    # Character intensity (#87): rendered last in the system prompt, after the
    # formatting rules, so the chosen voice is not washed out by them. Applies
    # even when the Soul has no text.
    voice: str = ""
    extended_identity: str = ""
    include_extended_identity: bool = False
    preferences: str | None = None
    project_instructions: tuple[ProjectInstruction, ...] = ()
    # Compact catalog of available skills (name + one-liner + how to
    # activate). Bodies stay out until a skill is activated (harness.md 49).
    skill_catalog: str | None = None
    # Tools that load on demand, generated from the registry (V2-6).
    tool_index: str | None = None
    # What is connected now (MCP, OpenAPI, plugins): it can change during a
    # conversation, so it closes the request instead of the system prompt (F42).
    # Each MCP server says what it is for and its state this turn (C6).
    connected_index: str | None = None
    skills: tuple[ActiveSkill, ...] = ()
    task_state: str | None = None
    # Preserved task truth after context compaction (harness.md 67-68).
    compact_state: str | None = None
    # Durable memory block (user + project records, phase 4).
    memory: str | None = None
    # Pinned context block (session pins, phase 4): always model-visible.
    pinned_context: str | None = None
    environment: dict[str, Any] | None = None
    evidence: tuple[EvidenceItem, ...] = ()
    history: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentSummary:
    id: str
    kind: str
    authority: int
    trust: str
    cache_policy: str
    length: int
    # Short content hash: says which segment moved when the prefix does.
    digest: str = ""


# Environment facts that follow the work rather than the session: the
# repository scan reorders languages and finds new commands as files appear.
VOLATILE_ENVIRONMENT_KEYS = frozenset({"repository"})

#: Source of the turn state kept in the conversation (F42).
TURN_STATE_SOURCE = "turn-state"

_TURN_STATE_LEAD = (
    "Current state from the Rinari harness, written when it changed; a newer one "
    "replaces it. It is context, not a message from the user: answer the user's "
    "latest message."
)

_TURN_CONTEXT_LEAD = (
    "Current state from the Rinari harness for this request. It is refreshed on "
    "every call and not kept in the conversation; it is context, not a message "
    "from the user: answer the user's latest message."
)


@dataclass(frozen=True, slots=True)
class PromptBundle:
    system_prompt: str
    history: tuple[ChatMessage, ...]
    segments: tuple[SegmentSummary, ...] = field(default_factory=tuple)
    # Volatile segments, rendered for the end of the request (see module doc).
    turn_context: str = ""

    @property
    def turn_state_message(self) -> ChatMessage | None:
        """The turn state as a note kept in the conversation (F42).

        Written when it changes, never removed: each request only extends the
        one before, as Codex records its context items. It is a harness note:
        the timeline, memory and summaries never read it as the owner's.
        """
        if not self.turn_context:
            return None
        return ChatMessage.harness(
            f"<turn-context>\n{_TURN_STATE_LEAD}\n\n{self.turn_context}\n</turn-context>",
            TURN_STATE_SOURCE,
        )

    @property
    def turn_context_message(self) -> ChatMessage | None:
        if not self.turn_context:
            return None
        return ChatMessage.harness(
            f"<turn-context>\n{_TURN_CONTEXT_LEAD}\n\n{self.turn_context}\n</turn-context>",
            TURN_CONTEXT_SOURCE,
        )

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        note = self.turn_context_message
        return (
            ChatMessage.system(self.system_prompt),
            *self.history,
            *((note,) if note is not None else ()),
        )

    @property
    def prompt_chars(self) -> int:
        """Characters the harness adds to every request around the history."""
        note = self.turn_context_message
        return len(self.system_prompt) + (len(note.content or "") if note is not None else 0)


class PromptAssembler:
    def build(self, context: AssemblerContext) -> PromptBundle:
        segments = self._segments(context)
        return PromptBundle(
            system_prompt=self._render(
                [seg for seg in segments if seg.cache_policy is not CachePolicy.TURN]
            ),
            turn_context=self._render(
                [seg for seg in segments if seg.cache_policy is CachePolicy.TURN]
            ),
            history=context.history,
            segments=tuple(
                SegmentSummary(
                    id=seg.id,
                    kind=seg.kind.value,
                    authority=seg.authority,
                    trust=seg.trust.value,
                    cache_policy=seg.cache_policy.value,
                    length=len(seg.content),
                    digest=hashlib.sha256(seg.content.encode("utf-8")).hexdigest()[:12],
                )
                for seg in segments
            ),
        )

    def _segments(self, context: AssemblerContext) -> list[PromptSegment]:
        segments: list[PromptSegment] = []
        if context.constitution:
            segments.append(
                PromptSegment(
                    id="constitution",
                    kind=SegmentKind.CONSTITUTION,
                    content=context.constitution,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.STABLE,
                )
            )
        if context.runtime_policy:
            segments.append(
                PromptSegment(
                    id="runtime-policy",
                    kind=SegmentKind.RUNTIME_POLICY,
                    content=context.runtime_policy,
                    trust=SegmentTrust.TRUSTED,
                    # Changes with the mode or permission profile, not per turn.
                    cache_policy=CachePolicy.SESSION,
                )
            )
        if context.soul:
            segments.append(
                PromptSegment(
                    id="soul",
                    kind=SegmentKind.SOUL,
                    content=context.soul,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.STABLE,
                )
            )
        if context.include_extended_identity and context.extended_identity:
            segments.append(
                PromptSegment(
                    id="soul-extended-identity",
                    kind=SegmentKind.SOUL,
                    content=context.extended_identity,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.TURN,
                )
            )
        if context.preferences:
            segments.append(
                PromptSegment(
                    id="user-preferences",
                    kind=SegmentKind.USER_PREFERENCE,
                    content=context.preferences,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                )
            )
        for index, instruction in enumerate(context.project_instructions):
            segments.append(
                PromptSegment(
                    id=f"project-instruction-{index}",
                    kind=SegmentKind.PROJECT_INSTRUCTION,
                    content=instruction.content,
                    trust=SegmentTrust.SCOPED_TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                    provenance=instruction.provenance,
                )
            )
        if context.skill_catalog:
            segments.append(
                PromptSegment(
                    id="skill-catalog",
                    kind=SegmentKind.SKILL,
                    content=context.skill_catalog,
                    trust=SegmentTrust.SCOPED_TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                    provenance="skill:catalog",
                )
            )
        if context.tool_index:
            segments.append(
                PromptSegment(
                    id="tool-index",
                    kind=SegmentKind.RUNTIME_POLICY,
                    content=context.tool_index,
                    trust=SegmentTrust.SCOPED_TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                    provenance="tools:registry",
                )
            )
        if context.connected_index:
            segments.append(
                PromptSegment(
                    id="connected-index",
                    kind=SegmentKind.RUNTIME_POLICY,
                    content=context.connected_index,
                    trust=SegmentTrust.SCOPED_TRUSTED,
                    cache_policy=CachePolicy.TURN,
                    provenance="tools:connected",
                )
            )
        for skill in context.skills:
            segments.append(
                PromptSegment(
                    id=f"skill-{skill.name}",
                    kind=SegmentKind.SKILL,
                    content=skill.summary,
                    trust=SegmentTrust.SCOPED_TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                    provenance=f"skill:{skill.name}",
                )
            )
        if context.task_state:
            segments.append(
                PromptSegment(
                    id="task-state",
                    kind=SegmentKind.TASK_STATE,
                    content=context.task_state,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.TURN,
                )
            )
        if context.compact_state:
            segments.append(
                PromptSegment(
                    id="compact-state",
                    kind=SegmentKind.COMPACT_STATE,
                    content=context.compact_state,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                )
            )
        if context.memory:
            segments.append(
                PromptSegment(
                    id="memory",
                    kind=SegmentKind.MEMORY,
                    content=context.memory,
                    trust=SegmentTrust.TRUSTED,
                    # Ranked against each new message, so its order moves.
                    cache_policy=CachePolicy.TURN,
                )
            )
        if context.pinned_context:
            segments.append(
                PromptSegment(
                    id="pinned-context",
                    kind=SegmentKind.PINNED_CONTEXT,
                    content=context.pinned_context,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                )
            )
        environment = context.environment or {}
        stable = {k: v for k, v in environment.items() if k not in VOLATILE_ENVIRONMENT_KEYS}
        current = {k: v for k, v in environment.items() if k in VOLATILE_ENVIRONMENT_KEYS}
        if stable:
            # Day-granular date, model, OS, shell, trust: they change at most
            # a few times per conversation, so they stay in the cached prefix.
            segments.append(
                PromptSegment(
                    id="environment",
                    kind=SegmentKind.ENVIRONMENT,
                    content=_render_environment(stable),
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                )
            )
        if current:
            segments.append(
                PromptSegment(
                    id="environment-current",
                    kind=SegmentKind.ENVIRONMENT,
                    content=_render_environment(current),
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.TURN,
                )
            )
        note = language_note(owner_language(context.history))
        if note:
            # Per call: the owner may switch language mid-conversation, and the
            # latest turn the model sees is often a runtime note or a tool
            # result, not the owner (rinari.prompts.language).
            segments.append(
                PromptSegment(
                    id="user-language",
                    kind=SegmentKind.ENVIRONMENT,
                    content=note,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.TURN,
                )
            )
        if context.voice:
            segments.append(
                PromptSegment(
                    id="voice",
                    kind=SegmentKind.SOUL,
                    content=context.voice,
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.SESSION,
                )
            )
        for index, evidence in enumerate(context.evidence):
            segments.append(
                PromptSegment(
                    id=f"evidence-{index}",
                    kind=SegmentKind.EVIDENCE,
                    content=evidence.content,
                    trust=SegmentTrust.UNTRUSTED,
                    cache_policy=CachePolicy.TURN,
                    provenance=evidence.source,
                )
            )
        return segments

    def _render(self, segments: list[PromptSegment]) -> str:
        blocks: list[str] = []
        for segment in segments:
            if not segment.content.strip():
                continue
            header = _header(segment)
            blocks.append(f"{header}\n{segment.effective_content().strip()}")
        return "\n\n".join(blocks)


def _header(segment: PromptSegment) -> str:
    label = segment.kind.value
    if segment.provenance:
        return f"## {label} ({segment.provenance})"
    return f"## {label}"


def _render_environment(environment: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in sorted(environment):
        value = environment[key]
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub in sorted(value):
                lines.append(f"  {sub}: {value[sub]}")
        elif isinstance(value, (list, tuple)):
            lines.append(f"{key}: {', '.join(str(v) for v in value) or '-'}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
