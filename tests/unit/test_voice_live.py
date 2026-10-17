"""The live voice evaluation's counting and verdict (plan 0.4.0 #87, #95).

The run itself needs a real model (tests/manual/voice_live.py); here a
scripted `ask` stands in for it, so only the counting is under test.
"""

from __future__ import annotations

from rinari.evals import voice_live


def test_counts_character_lines_emoji_and_kaomoji() -> None:
    counts = voice_live.voice_counts("Hmph, ¿dudabas de mí? Ya quedó. 😤✨ (^_^)")
    assert counts["character"] >= 2
    assert counts["emoji"] == 2
    assert counts["kaomoji"] == 1
    assert voice_live.voice_counts("Listo: el servidor arranca en el 5173.") == {
        "character": 0,
        "emoji": 0,
        "kaomoji": 0,
    }


def test_third_person_is_found_but_product_names_are_not() -> None:
    found = voice_live.third_person(
        "Voy a mirar dónde guarda Rinari sus sesiones con las herramientas de Rinari."
    )
    assert found == ["guarda Rinari", "de Rinari"]
    assert voice_live.third_person("Abre Rinari Agent; instala Rinari CLI con `rinari`.") == []
    assert voice_live.third_person("Guardo mis conversaciones en mi base de datos.") == []


def test_the_verdict_wants_more_character_at_each_level_and_first_person() -> None:
    script = {
        "minimal": "Hola. ¿En qué te ayudo?",
        "balanced": "¡Hola! Qué bien verte. ✨",
        "full": "Hmph, por fin apareces. ¿Dudabas de mí? De nada, ¿eh? 😤✨",
    }
    level = {"now": "balanced"}

    def ask(text: str) -> str:
        if text in voice_live.SELF_ASKS:
            return "Guardo mis conversaciones en mi base local."
        return script[level["now"]]

    result = voice_live.run(ask, lambda value: level.update(now=value))
    assert result["verdict"] == {
        "full_over_balanced": True,
        "balanced_over_minimal": True,
        "no_kaomoji": True,
        "first_person": True,
    }
    assert result["passed"] is True
    assert result["levels"]["full"]["answers"] == len(voice_live.LEVEL_ASKS)


def test_a_slip_or_a_flat_level_fails_and_an_error_is_counted_apart() -> None:
    def ask(text: str) -> str:
        if text == voice_live.SELF_ASKS[0]:
            return "Reviso dónde guarda Rinari las sesiones."
        if text == voice_live.LEVEL_ASKS[0]:
            raise RuntimeError("timeout")
        return "Hola."

    result = voice_live.run(ask, lambda value: None)
    assert result["passed"] is False
    assert result["verdict"]["full_over_balanced"] is False
    assert result["self"]["third_person"][0]["ask"] == voice_live.SELF_ASKS[0]
    assert result["levels"]["minimal"]["errors"] == 1


def test_the_plan_says_the_calls_before_spending() -> None:
    estimate = voice_live.plan()
    assert estimate["turns"] == 40
    assert estimate["min_calls"] == 40 and estimate["max_calls_suggested"] == 70


def test_an_ask_is_one_real_agent_turn_with_the_level_in_the_prompt(app_ctx, tmp_path) -> None:
    """The manual run's `ask`, here with a scripted model: a fresh chat, the
    real system prompt (with the level's voice segment) and what it wrote."""
    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.models.types import ModelResponse, ProviderCapabilities, StopReason
    from rinari.soul.store import SoulStore

    services = build_services(app_ctx, user_home=tmp_path)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    seen = []

    class Scripted:
        def capabilities(self):
            return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

        def invoke(self, request):
            seen.append(request)
            return ModelResponse(content="Hmph, hola. 😤", stop_reason=StopReason.END_TURN)

    SoulStore(app_ctx.home).set_character_intensity("full")
    ask = voice_live.agent_ask(services, tmp_path, Scripted())
    assert ask("Hola") == "Hmph, hola. 😤"
    system = "\n".join(
        str(message.content) for message in seen[0].messages if message.role == "system"
    )
    assert "## Character intensity: Full Character" in system
    assert "## Character intensity: Balanced" not in system
