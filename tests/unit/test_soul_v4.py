"""Soul 4.0: bundled default voice, character intensity and migration rules."""

from __future__ import annotations

import itertools
import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_assembler_context
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.prompts.soul_sections import split_soul
from rinari.runtime.identity import load_active_soul, load_soul
from rinari.soul.intensity import INTENSITIES, intensity_instructions
from rinari.soul.store import DEFAULT_SOUL_ID, SoulStore


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    container.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    container.models.add("fake", "fake-model-1", "fake-one")
    container.providers.use("fake")
    return container


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = itertools.count(1)


def _call(server, method, params=None):
    line = {"id": f"q{next(_ids)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _session(server, tmp_path) -> str:
    response = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    assert response["ok"], response
    return response["result"]["session"]["id"]


def test_bundled_default_is_soul_4(services) -> None:
    default = SoulStore(services.ctx.home).get(DEFAULT_SOUL_ID)
    assert default.version == "4.0"
    identity = default.identity
    assert identity.startswith("You are Rinari,")
    # Invariants survive the new voice.
    for marker in (
        "You know you are an AI",
        "accuracy wins",
        "Never fake execution",
        "Never bend a test result",
        "Never claim to be human",
        "No manipulation, guilt-tripping, possessiveness, exclusivity pressure",
        "never changes permissions, approvals, security rules",
        "Drop the act completely",
    ):
        assert marker in identity, marker
    # The voice is concrete: where it stays out, and in/out-of-character examples.
    assert "## Where the character stays out" in identity
    assert "Generic chatbot (avoid)" in identity
    assert "Over the top (avoid)" in identity
    assert "Listo. Y sí, quedó bonito." in identity
    # Nothing owner-specific is hardcoded in the default persona.
    assert "Xainner" not in identity


def test_canonical_reference_soul_is_4(services) -> None:
    asset = load_soul(services.ctx.home)
    assert asset.version == "4.0"
    canonical, _ = split_soul(asset.text)
    assert "No kaomoji." not in canonical
    assert "character-intensity setting" in canonical


def test_intensity_levels_are_visibly_different() -> None:
    blocks = {level: intensity_instructions(level) for level in INTENSITIES}
    assert len(set(blocks.values())) == 3
    assert "no emoji or kaomoji" in blocks["minimal"]
    assert "every conversational reply" in blocks["full"]
    # Unknown values fall back to the default rather than failing a turn.
    assert intensity_instructions("loud") == blocks["balanced"]


def test_intensity_reaches_the_main_prompt(services, server, tmp_path) -> None:
    session_id = _session(server, tmp_path)
    record = services.sessions.show(session_id)
    default_context = build_assembler_context(services, record)
    assert "Character intensity: Balanced" in default_context.soul
    assert default_context.soul.startswith("You are Rinari,")
    SoulStore(services.ctx.home).set_character_intensity("full")
    full = build_assembler_context(services, record).soul
    assert "Character intensity: Full Character" in full
    SoulStore(services.ctx.home).set_character_intensity("minimal")
    minimal = build_assembler_context(services, record).soul
    assert "Character intensity: Minimal" in minimal
    assert minimal != full


def test_intensity_applies_to_custom_souls_too(services, server, tmp_path) -> None:
    store = SoulStore(services.ctx.home)
    store.create("mine", name="Mine", identity="You are Mine, a custom persona.")
    store.activate("mine")
    store.set_character_intensity("minimal")
    session_id = _session(server, tmp_path)
    soul = build_assembler_context(services, services.sessions.show(session_id)).soul
    assert soul.startswith("You are Mine, a custom persona.")
    assert "Character intensity: Minimal" in soul


def test_settings_protocol(server) -> None:
    assert CAPABILITIES["soul_character_intensity_v1"] is True
    got = _call(server, "soul.settings.get")
    assert got["result"]["settings"] == {
        "character_intensity": "balanced",
        "options": ["minimal", "balanced", "full"],
        "default": "balanced",
    }
    updated = _call(server, "soul.settings.set", {"character_intensity": "full"})
    assert updated["result"]["settings"]["character_intensity"] == "full"
    assert _call(server, "soul.settings.get")["result"]["settings"]["character_intensity"] == "full"
    for bad in ({}, {"character_intensity": "max"}, {"character_intensity": 3}):
        error = _call(server, "soul.settings.set", bad)
        assert error["ok"] is False
        assert error["error"]["code"] in ("INVALID_PARAMS", "INVALID_USAGE")


def test_corrupt_settings_fall_back_to_default(services) -> None:
    (services.ctx.home / "soul_settings.toml").write_text("not = [valid", encoding="utf-8")
    assert SoulStore(services.ctx.home).character_intensity() == "balanced"


def test_custom_and_legacy_souls_are_never_overwritten(services) -> None:
    home = services.ctx.home
    store = SoulStore(home)
    store.create("rinari-mine", name="Rinari (mine)", identity="You are my own Rinari.")
    store.activate("rinari-mine")
    assert load_active_soul(home).text == "You are my own Rinari."
    assert store.get("rinari-mine").version == "1.0"
    # Legacy override keeps winning over the bundled default once deactivated.
    (home / "active_soul").unlink()
    (home / "soul.md").write_text("Legacy custom soul.", encoding="utf-8")
    assert load_active_soul(home).text == "Legacy custom soul."
