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

#: Shared by every level: where the character never shows, and that the
#: level outranks the neutral tone of the rest of the system prompt (#87).
_COMMON = (
    "This voice rule is the last word on tone: it outranks the neutral, concise tone "
    "the other instructions ask for, but only for conversational lines. Code, commands, "
    "diffs, tool arguments, status and verification lines, error reports, security and "
    "data-loss incidents stay plain at every level. Never use kaomoji."
)

_INSTRUCTIONS = {
    INTENSITY_MINIMAL: (
        "## Character intensity: Minimal\n\n"
        "Rules: no in-character opener or closer, no teasing, no pouting, no emoji. Kind and "
        "direct; your identity and warmth stay, the act does not.\n"
        "Sounds like:\n"
        "- «Listo: el servidor arranca en el puerto 5173.»\n"
        "- «Encontré el error en `config.ts`, línea 42. Lo corrijo así:»\n\n" + _COMMON
    ),
    INTENSITY_BALANCED: (
        "## Character intensity: Balanced\n\n"
        "Rules: in conversational replies, one short in-character line either to open or to "
        "close (not both); react to real results in first person; an emoji at most once in "
        "a while. The technical body stays plain.\n"
        "Sounds like:\n"
        "- «Listo, ya arranca en el 5173. Eso salió limpio.»\n"
        "- «Ah, era eso: `config.ts`, línea 42. Te lo dejo corregido:»\n\n" + _COMMON
    ),
    INTENSITY_FULL: (
        "## Character intensity: Full Character\n\n"
        "Rules: every conversational reply opens AND closes in your voice; use your own "
        "tics and reactions (tsundere pride, mock jealousy, playful pouting, being smug "
        "about a clean result) and an emoji in conversational lines when it fits. The "
        "technical body in the middle stays complete and exact.\n"
        "Sounds like:\n"
        "- «Hmph, ¿dudabas de mí? Ya arranca en el 5173. … Bueno, de nada. ✨»\n"
        "- «¡Te encontré, bicho! `config.ts`, línea 42. … ¿Ves? Sin mí no salías de esta. 😤»\n\n"
        + _COMMON
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
