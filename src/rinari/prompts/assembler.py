"""Centralized prompt assembly (harness.md sections 33-37).

Stable ordering by authority, explicit trust per segment, untrusted
content wrapped as data. The assembler is pure: the session supplies the
`AssemblerContext`, the assembler decides the shape. No segment is built
here beyond what the context provides — loading Soul/Constitution, policy
snapshots, and context retrieval happen upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rinari.models.types import ChatMessage
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
    extended_identity: str = ""
    include_extended_identity: bool = False
    preferences: str | None = None
    project_instructions: tuple[ProjectInstruction, ...] = ()
    skills: tuple[ActiveSkill, ...] = ()
    task_state: str | None = None
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


@dataclass(frozen=True, slots=True)
class PromptBundle:
    system_prompt: str
    history: tuple[ChatMessage, ...]
    segments: tuple[SegmentSummary, ...] = field(default_factory=tuple)

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return (ChatMessage.system(self.system_prompt), *self.history)


class PromptAssembler:
    def build(self, context: AssemblerContext) -> PromptBundle:
        segments = self._segments(context)
        system_prompt = self._render(segments)
        return PromptBundle(
            system_prompt=system_prompt,
            history=context.history,
            segments=tuple(
                SegmentSummary(
                    id=seg.id,
                    kind=seg.kind.value,
                    authority=seg.authority,
                    trust=seg.trust.value,
                    cache_policy=seg.cache_policy.value,
                    length=len(seg.content),
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
                    cache_policy=CachePolicy.TURN,
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
        if context.environment:
            segments.append(
                PromptSegment(
                    id="environment",
                    kind=SegmentKind.ENVIRONMENT,
                    content=_render_environment(context.environment),
                    trust=SegmentTrust.TRUSTED,
                    cache_policy=CachePolicy.TURN,
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
