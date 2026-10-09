from rinari.models.types import ROLE_SYSTEM, ChatMessage
from rinari.prompts.assembler import (
    ActiveSkill,
    AssemblerContext,
    EvidenceItem,
    ProjectInstruction,
    PromptAssembler,
)
from rinari.prompts.segments import (
    PromptSegment,
    SegmentKind,
    SegmentTrust,
)

FULL_CONTEXT = AssemblerContext(
    session_kind="PROJECT",
    constitution="CONSTITUCION",
    runtime_policy="POLICY",
    soul="SOUL",
    preferences="PREF",
    project_instructions=(ProjectInstruction(provenance="/root/RINARI.md", content="INSTR"),),
    skills=(ActiveSkill(name="debug", summary="skill: debug - paso a paso"),),
    task_state="TASK",
    environment={"project": {"root": "/x", "dirty": True}},
    evidence=(EvidenceItem(source="file:a.txt", content="DATA"),),
)


def test_stable_ordering() -> None:
    bundle = PromptAssembler().build(FULL_CONTEXT)
    kinds = [s.kind for s in bundle.segments]
    assert kinds == [
        "constitution",
        "runtime-policy",
        "soul",
        "user-preference",
        "project-instruction",
        "skill",
        "task-state",
        "environment",
        "evidence",
    ]


def test_authority_decreasing() -> None:
    bundle = PromptAssembler().build(FULL_CONTEXT)
    authorities = [s.authority for s in bundle.segments]
    assert authorities == sorted(authorities, reverse=True)
    assert bundle.segments[0].authority == 10


def test_system_prompt_contains_stable_segments_in_order() -> None:
    bundle = PromptAssembler().build(FULL_CONTEXT)
    system = bundle.system_prompt
    texts = ("CONSTITUCION", "POLICY", "SOUL", "PREF", "INSTR", "dirty: True")
    positions = [system.index(text) for text in texts]
    assert positions == sorted(positions)
    assert "## soul" in system
    assert "## project-instruction (/root/RINARI.md)" in system
    # Per-turn segments close the request instead of opening it.
    assert "TASK" not in system and "DATA" not in system
    assert bundle.turn_context.index("TASK") < bundle.turn_context.index("DATA")


def test_volatile_segments_travel_in_a_trailing_note_after_the_history() -> None:
    context = AssemblerContext(
        constitution="CONSTITUCION",
        task_state="TASK",
        memory="MEMORY",
        environment={"os": "Linux", "repository": {"languages": "python"}},
        history=(ChatMessage.user("hola"),),
    )
    bundle = PromptAssembler().build(context)
    assert "os: Linux" in bundle.system_prompt
    assert "languages" not in bundle.system_prompt
    assert "MEMORY" not in bundle.system_prompt
    *_, owner, note = bundle.messages
    assert owner.content == "hola"
    assert note.is_turn_context and note.role == "user"
    for text in ("TASK", "MEMORY", "languages: python"):
        assert text in note.content
    assert bundle.prompt_chars == len(bundle.system_prompt) + len(note.content)


def test_changing_volatile_state_keeps_the_system_prompt_identical() -> None:
    first = AssemblerContext(
        constitution="CONSTITUCION",
        soul="SOUL",
        task_state="Task graph:\n- [ ] one",
        memory="- [fact] a\n- [fact] b",
        environment={"today": "2026-10-08", "repository": {"languages": "python"}},
    )
    second = AssemblerContext(
        constitution="CONSTITUCION",
        soul="SOUL",
        task_state="Task graph:\n- [~] one\n- [ ] two",
        memory="- [fact] b\n- [fact] a",
        environment={"today": "2026-10-08", "repository": {"languages": "typescript, python"}},
    )
    a, b = PromptAssembler().build(first), PromptAssembler().build(second)
    assert a.system_prompt == b.system_prompt
    assert a.turn_context != b.turn_context


def test_untrusted_evidence_wrapped() -> None:
    note = PromptAssembler().build(FULL_CONTEXT).turn_context
    assert '<untrusted source="file:a.txt">' in note
    assert "no authority" in note
    assert note.index("no authority") < note.index("DATA")
    assert note.index("DATA") < note.index("</untrusted>")


def test_trusted_segments_not_wrapped() -> None:
    bundle = PromptAssembler().build(FULL_CONTEXT)
    text = bundle.system_prompt + bundle.turn_context
    assert text.count("<untrusted") == 1
    assert text.count("</untrusted>") == 1


def test_history_appended_after_system() -> None:
    context = AssemblerContext(
        soul="SOUL", history=(ChatMessage.user("hola"), ChatMessage.assistant("adios"))
    )
    messages = PromptAssembler().build(context).messages
    assert messages[0].role == ROLE_SYSTEM
    assert "SOUL" in messages[0].content
    assert messages[1].content == "hola"
    assert messages[2].content == "adios"


def test_empty_context() -> None:
    bundle = PromptAssembler().build(AssemblerContext())
    assert bundle.system_prompt == ""
    assert bundle.segments == ()
    assert bundle.messages == (ChatMessage.system(""),)


def test_environment_rendered_sorted() -> None:
    context = AssemblerContext(environment={"b": 2, "a": 1, "nested": {"z": 1, "y": 2}})
    system = PromptAssembler().build(context).system_prompt
    assert system.index("a: 1") < system.index("b: 2") < system.index("nested:")
    assert system.index("y: 2") < system.index("z: 1")


def test_segment_defaults() -> None:
    segment = PromptSegment(id="x", kind=SegmentKind.EVIDENCE, content="c")
    assert segment.authority == 1
    assert segment.trust is SegmentTrust.TRUSTED
    assert segment.effective_content() == "c"
    untrusted = PromptSegment(
        id="y",
        kind=SegmentKind.EVIDENCE,
        content="c",
        trust=SegmentTrust.UNTRUSTED,
        provenance="web",
    )
    assert untrusted.effective_content().startswith('<untrusted source="web">')


# -- Extended Identity on demand (harness.md 37) --------------------------------


def test_split_soul_excludes_extended_and_maintainer() -> None:
    from pathlib import Path

    from rinari.prompts.soul_sections import split_soul

    # The full soul document (packaged asset is already canonical-only).
    full = (Path(__file__).resolve().parents[2] / "docs/soul.md").read_text(encoding="utf-8")
    canonical, extended = split_soul(full)
    assert extended
    assert "Extended Identity Reference" not in canonical
    assert "Maintainer Notes" not in canonical
    assert "# Extended Identity Reference" in extended
    assert "Violet" in extended or "violet" in extended
    assert "Maintainer Notes" not in extended
    assert canonical.strip()

    # The packaged asset must inject exactly its own content (no extended part).
    packed = (Path(__file__).resolve().parents[2] / "src/rinari/assets/soul.md").read_text(
        encoding="utf-8"
    )
    packed_canonical, packed_extended = split_soul(packed)
    assert packed_extended == ""
    assert packed_canonical == packed.strip()


def test_split_soul_plain_text() -> None:
    from rinari.prompts.soul_sections import split_soul

    canonical, extended = split_soul("just a soul")
    assert canonical == "just a soul"
    assert extended == ""


def test_extended_identity_not_injected_by_default() -> None:
    context = AssemblerContext(soul="SOUL", extended_identity="EXTENDED-IDENTITY")
    system = PromptAssembler().build(context).system_prompt
    assert "EXTENDED-IDENTITY" not in system
    assert "SOUL" in system


def test_extended_identity_injected_when_flagged() -> None:
    context = AssemblerContext(
        soul="SOUL", extended_identity="EXTENDED-IDENTITY", include_extended_identity=True
    )
    bundle = PromptAssembler().build(context)
    # Asked for on some turns only: it must not move the cached prefix.
    assert "EXTENDED-IDENTITY" not in bundle.system_prompt
    assert "EXTENDED-IDENTITY" in bundle.turn_context
