"""Tests para la identidad de Rinari (SOUL.md canónico)."""

from rinari.identity import (
    SOUL_PATH,
    build_agent_prompt,
    build_chat_prompt,
    load_soul,
)


def test_soul_file_exists():
    """El SOUL.md canónico existe en assets."""
    assert SOUL_PATH.exists()
    text = SOUL_PATH.read_text(encoding="utf-8")
    assert "Rinari" in text
    assert "maid" in text.lower()


def test_load_soul_returns_text():
    """load_soul devuelve el contenido del archivo."""
    text = load_soul()
    assert len(text) > 500
    assert "Identidad" in text


def test_build_chat_prompt_includes_core():
    """El prompt de chat incluye identidad, núcleo y voz."""
    prompt = build_chat_prompt()
    assert "Rinari" in prompt
    assert "terminal" in prompt
    assert "NUNCA digas" in prompt or "nunca digas" in prompt.lower()
    assert "Xainner" in prompt  # se dirige por su nombre


def test_chat_prompt_has_no_mode_frames():
    """Sin modos: el prompt no menciona 'modo waifu' ni 'modo serio'."""
    prompt = build_chat_prompt()
    assert "modo actual" not in prompt.lower()
    assert "waifu" not in prompt.lower().split("modo")[0]  # sin frames de modo


def test_build_agent_prompt_includes_rules():
    """El prompt del agente incluye las reglas de trabajo técnico."""
    prompt = build_agent_prompt()
    assert "planifica" in prompt.lower() or "planificas" in prompt.lower()
    assert "tests" in prompt.lower()
    assert "archivos" in prompt.lower()


def test_agent_prompt_keeps_personality():
    """El agente conserva la voz (humor, kaomoji) + reglas técnicas."""
    prompt = build_agent_prompt()
    assert "tests" in prompt.lower()
    assert "humor" in prompt.lower() or "bromas" in prompt.lower()


def test_prompt_composable_identically():
    """Chat y agente comparten el mismo SOUL (misma base de identidad)."""
    chat = build_chat_prompt()
    agent = build_agent_prompt()
    # ambos incluyen la sección Identidad
    assert "Identidad" in chat
    assert "Identidad" in agent
    # el agente agrega las reglas, el chat no las necesita
    assert "Modo agente" in agent
