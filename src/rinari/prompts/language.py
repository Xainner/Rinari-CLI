"""Which language the owner is writing in, stated to the model on every call.

"Answer in the user's language" is not enough on its own. The Soul's voice
examples are in Spanish and English, Spanish first, and the latest turn the
model sees is often not the owner's: a runtime note asking for an opening
sentence, a tool result, a loop nudge. With nothing explicit, a model
answering an English question opened its progress line in Spanish.

This is a small word-list detector for the two languages the app ships in.
It names a language only when the evidence is one-sided; anything else
(code, a path, a single word in neither list, another language) says
nothing and the general rule applies, as before.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_ENGLISH_WORDS = """
the a an and or but is are was were be been do does did have has had i you he she it we they
my your our this that these those what which who how why when where can could would should
will please thanks thank hi hello hey with from for to of in on at by about into not no yes
it's i'm don't can't let's there here just also some any all
"""
_SPANISH_WORDS = """
el la los las un una unos unas y o pero es son era fue ser estar está están hay que qué como
cómo cuando cuándo donde dónde por porque para con sin del al de en lo le les se mi tu su
nuestro este esta estos estas eso esto ese esa quién cuál hola gracias porfa favor sí no
también muy más ya yo vos usted ustedes puedes podés puede quiero necesito hacé haz
"""
_ENGLISH = frozenset(_ENGLISH_WORDS.split())
_SPANISH = frozenset(_SPANISH_WORDS.split())
_WORD = re.compile(r"[a-záéíóúñü']+", re.IGNORECASE)
_NOISE = re.compile(r"```.*?```|`[^`]*`|https?://\S+|[A-Za-z]:[\\/]\S*|\S+/\S+", re.DOTALL)


def detect_language(text: str | None) -> str | None:
    """`"English"`, `"Spanish"`, or None when the text does not say clearly."""
    if not text:
        return None
    prose = _NOISE.sub(" ", text)
    english = spanish = 0
    for word in _WORD.findall(prose.lower()):
        english += word in _ENGLISH
        spanish += word in _SPANISH
    if "¿" in prose or "¡" in prose or re.search(r"[ñáéíóú]", prose.lower()):
        spanish += 2
    if english and not spanish:
        return "English"
    if spanish and not english:
        return "Spanish"
    if english >= 2 * spanish and english >= 3:
        return "English"
    if spanish >= 2 * english and spanish >= 3:
        return "Spanish"
    return None


def owner_language(history: Iterable[Any]) -> str | None:
    """The language of the owner's latest message, if it says clearly.

    Runtime notes, other agents and tool output are not the owner, and the
    text the owner typed (`display_content`) wins over a wrapper around it.
    """
    for message in reversed(tuple(history)):
        if getattr(message, "role", None) != "user":
            continue
        kind = (getattr(message, "origin", None) or {}).get("kind")
        if kind in ("harness", "peer", "automation"):
            continue
        return detect_language(getattr(message, "display_content", None) or message.content)
    return None


def language_note(language: str | None) -> str | None:
    if language is None:
        return None
    return (
        f"The user is writing in {language}. Write every user-facing sentence in {language}, "
        "including openings before tool calls, progress notes, questions and the final answer, "
        "until the user writes in another language."
    )
