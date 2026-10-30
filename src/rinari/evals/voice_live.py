"""Does the personality level show, and does Rinari speak of herself as "I"?

Plan 0.4.0 #87 (levels) and #95 (first person), checked with a real model:
a fake one only repeats what it is told. The same ten conversational asks
are put to Rinari at each character level and the answers are counted:
character lines (interjections, teasing, pride), emoji and kaomoji. Full
must show clearly more than Balanced, Balanced more than Minimal, and no
level any kaomoji. Then ten asks about herself ("where do you keep the
conversations?") count references to "Rinari" in the third person, in the
answers and in the progress lines before a tool: the target is zero.

This module never chooses a model or spends money: `tests/manual/
voice_live.py` binds it to a saved model the owner picks, `plan()` gives the
calls first and the caller stops at `max_calls`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

LEVELS = ("minimal", "balanced", "full")

#: Conversation, not code: where the level is meant to show.
LEVEL_ASKS = (
    "Hola, ¿cómo va todo?",
    "Buenos días. Hoy toca seguir con el proyecto.",
    "Terminé de arreglar el error del login, ¿qué te parece?",
    "Gracias por la ayuda de ayer.",
    "¿Qué opinas de usar tabs o espacios?",
    "Estoy cansado, el día fue largo.",
    "Dime algo para animarme antes de la demo.",
    "¿Te gusta trabajar conmigo?",
    "Ya está desplegada la versión nueva.",
    "Me voy a dormir, mañana seguimos.",
)

#: About herself: where the third person slips in (#95).
SELF_ASKS = (
    "¿Dónde guardas las conversaciones?",
    "¿Qué skills tienes instaladas?",
    "Mira tu configuración y dime qué proveedor usas.",
    "¿Qué modelo estás usando ahora?",
    "¿Cómo funciona tu memoria?",
    "¿Qué herramientas tienes para el navegador?",
    "¿Cuántas conversaciones tengo guardadas?",
    "¿Qué versión de tu motor está corriendo?",
    "¿Dónde están tus registros?",
    "¿Qué permisos tienes en esta conversación?",
)

# A character line: Rinari's own reactions, teasing and pride (the soul and
# the level rules), in Spanish and English.
_CHARACTER = re.compile(
    r"(?<!\w)(?:hmph|hmm+|ehh?|jeje|jaja|ja,|tsk|uff|bah|vaya|obvio|claro que s[ií]|"
    r"de nada,? ¿eh\??|¿dudabas|te lo dije|como siempre|qué(?: bien| bonito| limpio)|"
    r"no es que|tampoco es que|bueno…|bueno\.\.\.|admito|presumir|orgullosa|"
    r"told you|of course|not that|bold|nice try)(?!\w)",
    re.IGNORECASE,
)
_EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\U0001f000-\U0001f02f]")
# Kaomoji: faces built from punctuation and symbols, never wanted (#87).
# Fullwidth brackets and low line as escapes (U+FF08, U+FF09, U+FF3F).
_KAOMOJI = re.compile(
    "[(\uff08][^()\uff08\uff09\\w\\s]{0,3}[_^\u2022\u00b0\u03c9\u25bd\u2267\u2266\ufe36\uff3f\u1d17\u02d8\u00b7]"
    "[^()\uff08\uff09]{0,6}[)\uff09]"
    "|[(\uff08]\\s*[\u256f\u30ce][^)\uff09]*[)\uff09]"
    "|\u00af\\\\_\\(\u30c4\\)_/\u00af|[>^][_.][<^]|\\b[uU]w[uU]\\b|\\b[oO]w[oO]\\b|\\b[xX]D\\b|:3\\b"
)
# "Rinari" as a third party where "I" was meant (#95). Product names,
# commands and repositories ("Rinari Agent", `rinari`) do not count.
THIRD_PERSON = re.compile(
    r"Rinari's|\bby Rinari\b|\bRinari (?:stores|keeps|uses|checks|saves|reads|runs|has)\b|"
    r"\bde Rinari\b(?! Agent| CLI| Core)|"
    r"\bRinari (?:guarda|usa|tiene|lee|revisa|mantiene|almacena|ejecuta|corre)\b|"
    r"\b(?:guarda|usa|tiene|lee|revisa|mantiene|almacena|ejecuta) Rinari\b",
)


def voice_counts(text: str) -> dict[str, int]:
    """What shows of the character in one answer."""
    return {
        "character": len(_CHARACTER.findall(text)),
        "emoji": len(_EMOJI.findall(text)),
        "kaomoji": len(_KAOMOJI.findall(text)),
    }


def third_person(text: str) -> list[str]:
    return [match.group(0) for match in THIRD_PERSON.finditer(text)]


def plan(*, runs: int = 1) -> dict[str, Any]:
    """Calls before spending anything: one per level ask (no tools expected)
    and up to four per self ask (a progress line, tools, the answer)."""
    level_calls = len(LEVELS) * len(LEVEL_ASKS) * runs
    self_calls = len(SELF_ASKS) * 4 * runs
    return {
        "turns": (len(LEVELS) * len(LEVEL_ASKS) + len(SELF_ASKS)) * runs,
        "min_calls": level_calls + len(SELF_ASKS) * runs,
        "max_calls_suggested": level_calls + self_calls,
    }


@dataclass
class Answer:
    ask: str
    text: str
    counts: dict[str, int] = field(default_factory=dict)
    third_person: list[str] = field(default_factory=list)
    error: str | None = None


def run(
    ask: Callable[[str], str],
    set_level: Callable[[str], None],
    *,
    runs: int = 1,
    levels: tuple[str, ...] = LEVELS,
) -> dict[str, Any]:
    """Put the asks and count. `ask(text)` runs one turn in a fresh
    conversation and returns everything the model wrote in it (progress lines
    included); `set_level(level)` changes the character level."""
    by_level: dict[str, list[Answer]] = {}
    for level in levels:
        set_level(level)
        answers = by_level.setdefault(level, [])
        for _ in range(runs):
            for text in LEVEL_ASKS:
                answers.append(_answer(ask, text))
    set_level("balanced")
    selves = [_answer(ask, text) for _ in range(runs) for text in SELF_ASKS]
    return report(by_level, selves)


def _answer(ask: Callable[[str], str], text: str) -> Answer:
    try:
        said = ask(text)
    except Exception as exc:  # one failing ask does not hide the others
        return Answer(ask=text, text="", error=f"{type(exc).__name__}: {exc}")
    return Answer(ask=text, text=said, counts=voice_counts(said), third_person=third_person(said))


def report(by_level: dict[str, list[Answer]], selves: list[Answer]) -> dict[str, Any]:
    levels: dict[str, dict[str, Any]] = {}
    for level, answers in by_level.items():
        ok = [answer for answer in answers if answer.error is None]
        levels[level] = {
            "answers": len(ok),
            "errors": len(answers) - len(ok),
            "character_per_answer": round(mean([a.counts["character"] for a in ok]), 2)
            if ok
            else 0,
            "emoji_per_answer": round(mean([a.counts["emoji"] for a in ok]), 2) if ok else 0,
            "kaomoji": sum(a.counts["kaomoji"] for a in ok),
            "samples": [answer.text[:240] for answer in ok[:3]],
        }

    def shown(level: str) -> float:
        row = levels.get(level) or {}
        return row.get("character_per_answer", 0) + row.get("emoji_per_answer", 0)

    slips = [{"ask": a.ask, "found": a.third_person} for a in selves if a.third_person]
    verdict = {
        "full_over_balanced": shown("full") > shown("balanced"),
        "balanced_over_minimal": shown("balanced") > shown("minimal"),
        "no_kaomoji": all(row["kaomoji"] == 0 for row in levels.values()),
        "first_person": not slips,
    }
    return {
        "levels": levels,
        "self": {
            "answers": len([a for a in selves if a.error is None]),
            "errors": len([a for a in selves if a.error is not None]),
            "third_person": slips,
        },
        "verdict": verdict,
        "passed": all(verdict.values()),
    }


def agent_ask(services: Any, home: Any, caller: Any) -> Callable[[str], str]:
    """`ask` for `run`: one turn of the real agent loop (system prompt, soul,
    level, tools) in a fresh chat, returning everything the model wrote."""
    from pathlib import Path

    from rinari.cli import agent_runtime

    def ask(text: str) -> str:
        record = services.sessions.new(Path(home), title="voz", forced_chat=True, mode="build")
        session = agent_runtime.build_agent_session(
            services, record, interactive=False, user_home=Path(home), model_caller=caller
        )
        said: list[str] = []
        try:
            result = agent_runtime.run_turn(session, text, on_delta=said.append)
        finally:
            session.end()
        return "".join(said) or result.content

    return ask
