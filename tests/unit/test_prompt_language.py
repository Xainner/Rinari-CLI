"""The owner's language is stated to the model on every call (rinari.prompts.language).

Found in real use with Claude Subscription and DeepSeek alike: with the app
and the prompt in English, the opening line before tool calls came out in
Spanish. The Soul's voice examples are Spanish first, and the latest turn the
model answered was a runtime note, not the owner.
"""

from __future__ import annotations

import pytest

from rinari.models.types import ChatMessage
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.prompts.language import detect_language, owner_language
from rinari.runtime.agent import _foreign_script


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What files are in this project and what does src/main.py print?", "English"),
        ("hi", "English"),
        ("¿Qué archivos tiene el proyecto?", "Spanish"),
        ("hola", "Spanish"),
        ("arregla el bug del login porfa", "Spanish"),
        ("`npm test` C:/repo/src/main.py https://example.com", None),
        ("", None),
        ("ok", None),
    ],
)
def test_detects_only_what_the_text_says_clearly(text, expected):
    assert detect_language(text) == expected


def test_the_owner_is_the_last_message_they_typed_not_a_runtime_note():
    history = (
        ChatMessage.user("What does main.py print?"),
        ChatMessage.harness("Escribí una o dos frases antes de llamar herramientas.", "guard"),
    )
    assert owner_language(history) == "English"
    typed = ChatMessage(
        role="user",
        content="Answer the user's question directly. The question is:\n\n¿qué imprime main.py?",
        display_content="¿qué imprime main.py?",
    )
    assert owner_language((typed,)) == "Spanish"


def test_every_call_tells_the_model_the_owner_language():
    bundle = PromptAssembler().build(
        AssemblerContext(
            soul='Voice example: "Listo. Y sí, quedó bonito."',
            history=(ChatMessage.user("How many lines does README.md have?"),),
        )
    )
    assert "The user is writing in English" in bundle.turn_context
    # Per call, outside the cached system prompt: the owner may switch.
    assert "The user is writing in" not in bundle.system_prompt


def test_nothing_is_said_when_the_language_is_unclear():
    bundle = PromptAssembler().build(AssemblerContext(history=(ChatMessage.user("ok"),)))
    assert "The user is writing in" not in bundle.turn_context


def test_a_spanish_answer_to_an_english_question_is_rewritten():
    history = (ChatMessage.user("What files are in this project and what do they do?"),)
    spanish = (
        "El proyecto tiene dos archivos: README.md y src/main.py. El primero es la "
        "descripción y el segundo imprime un saludo cuando lo ejecutás."
    )
    english = (
        "The project has two files: README.md and src/main.py. The first one is the "
        "description and the second one prints a greeting when you run it."
    )
    assert _foreign_script(history, spanish) is True
    assert _foreign_script(history, english) is False
    # A short line is not enough evidence to spend a rewrite on.
    assert _foreign_script(history, "Listo.") is False
