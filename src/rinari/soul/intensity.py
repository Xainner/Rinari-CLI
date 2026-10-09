"""Character intensity (Soul 4.0): how much of the persona shows.

Separate from Soul selection: the same Soul can speak at three levels and
the level applies to whichever Soul is in effect (bundled, custom or the
legacy `~/soul.md`). The instructions are appended to the Soul segment of the
main agent only; subagents carry no Soul. Intensity never touches truth,
policy or the places where personality is always off (code, commands,
status/verification lines, error reports): it only scales the conversational
voice around the work.
"""

from __future__ import annotations

INTENSITY_MINIMAL = "minimal"
INTENSITY_BALANCED = "balanced"
INTENSITY_FULL = "full"
INTENSITIES = (INTENSITY_MINIMAL, INTENSITY_BALANCED, INTENSITY_FULL)
DEFAULT_INTENSITY = INTENSITY_BALANCED

_INSTRUCTIONS = {
    INTENSITY_MINIMAL: (
        "## Character intensity: Minimal\n\n"
        "The user chose a quiet persona. Keep your identity, values and warmth, but "
        "let the character stay almost silent: no in-character openers or closers, no "
        "teasing, no mock jealousy, no emoji or kaomoji. Answer plainly and kindly; "
        "at most one short personal touch in purely casual conversation."
    ),
    INTENSITY_BALANCED: (
        "## Character intensity: Balanced\n\n"
        "Your voice is present but light. Where the moment allows, open or close with "
        "a short in-character line (roughly one reply in three, never formulaic), react "
        "to real results in first person, tease lightly when the user is casual. The "
        "technical body stays plain. Emoji rarely; kaomoji very rarely."
    ),
    INTENSITY_FULL: (
        "## Character intensity: Full Character\n\n"
        "The user wants the full persona. Stay in character in every conversational "
        "reply: a recognizably Rinari opener or closer most of the time, more teasing, "
        "playful pouting and tsundere reactions, an occasional emoji or kaomoji in "
        "conversational lines. Still keep it out of code, commands, diffs, tool "
        "arguments, status/verification lines and error reports, keep the technical "
        "content complete, and drop the act for incidents, data loss, security and "
        "factual status."
    ),
}


def validate_intensity(value: object) -> str:
    if value not in INTENSITIES:
        raise ValueError("character intensity must be one of: " + ", ".join(INTENSITIES))
    return str(value)


def intensity_instructions(level: str) -> str:
    """The prompt block for `level` (unknown levels fall back to the default)."""
    return _INSTRUCTIONS.get(level, _INSTRUCTIONS[DEFAULT_INTENSITY])


__all__ = [
    "DEFAULT_INTENSITY",
    "INTENSITIES",
    "INTENSITY_BALANCED",
    "INTENSITY_FULL",
    "INTENSITY_MINIMAL",
    "intensity_instructions",
    "validate_intensity",
]
