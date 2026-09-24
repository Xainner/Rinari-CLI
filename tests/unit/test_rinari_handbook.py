"""The rinari-handbook skill: an index read every turn, references on demand,
and its tools visible from the call that activates it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rinari.application.services import build_services
from rinari.capability_search import search_capabilities
from rinari.cli.agent_runtime import _exposure_for
from rinari.skills.manifest import validate_skill
from rinari.skills.tools import SkillToolHost, skill_tools
from rinari.tools.catalog import builtin_catalog
from rinari.tools.exposure import ToolExposure

HANDBOOK_TOOLS = {
    "rinari.status",
    "rinari.sessions",
    "rinari.session",
    "rinari.turn",
    "skills.read",
}


def _session(app_ctx, tmp_path):
    from rinari.application.provider_service import AddProviderInput

    services = build_services(app_ctx)
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
    cwd = tmp_path / "work"
    cwd.mkdir()
    return services, services.sessions.start(cwd, forced_chat=True).session


class _Ctx:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.exposure = ToolExposure()


def test_the_handbook_is_valid_small_and_ships_its_references(app_ctx):
    services = build_services(app_ctx)
    manifest = services.skills.get("rinari-handbook")
    assert manifest.source == "packaged"
    assert set(manifest.required_tools) == HANDBOOK_TOOLS
    assert validate_skill(manifest, set(builtin_catalog().names())) == []
    # Injected every turn while active: it must stay an index.
    assert len(manifest.body) < 4000
    assert services.skills.references("rinari-handbook") == [
        "references/cli.md",
        "references/desktop.md",
        "references/efficiency.md",
        "references/engine.md",
        "references/recipes.md",
    ]


def test_references_are_read_by_page_and_never_outside_the_skill(app_ctx):
    services = build_services(app_ctx)
    tools = {t.name: t for t in skill_tools(SkillToolHost(service=services.skills))}
    shown = tools["skills.show"].handler({"name": "rinari-handbook"}, None)
    assert "references/recipes.md" in shown.data["references"]

    page = tools["skills.read"].handler(
        {"name": "rinari-handbook", "path": "references/recipes.md", "limit": 3}, None
    )
    assert page.ok and page.data["offset"] == 0 and page.data["next_offset"] == 3
    assert page.data["text"].startswith("# Diagnostic recipes")
    rest = tools["skills.read"].handler(
        {"name": "rinari-handbook", "path": "references/recipes.md", "offset": 3}, None
    )
    assert rest.data["next_offset"] is None

    for path in ("../debug/SKILL.md", "C:/Windows/win.ini", "/etc/passwd", "SKILL.md"):
        denied = tools["skills.read"].handler({"name": "rinari-handbook", "path": path}, None)
        assert not denied.ok, path
    missing = tools["skills.read"].handler(
        {"name": "rinari-handbook", "path": "references/nope.md"}, None
    )
    assert missing.error.code.value == "NOT_FOUND"
    assert "references/recipes.md" in missing.error.message


def test_activating_the_skill_exposes_its_tools_in_the_same_call(app_ctx, tmp_path):
    services, record = _session(app_ctx, tmp_path)
    tools = {t.name: t for t in skill_tools(SkillToolHost(service=services.skills))}
    ctx = _Ctx(record.id)
    result = tools["skills.activate"].handler({"name": "rinari-handbook"}, ctx)
    assert result.ok and set(result.data["tools_activated"]) == HANDBOOK_TOOLS
    assert set(ctx.exposure.activated) >= HANDBOOK_TOOLS
    assert all(ctx.exposure.activated[n].scope == "session" for n in HANDBOOK_TOOLS)


def test_a_pinned_skill_keeps_its_tools_on_every_new_runtime(app_ctx, tmp_path):
    # The desktop builds a runtime per turn; the pin, not the old exposure,
    # decides what the next turn sees.
    services, record = _session(app_ctx, tmp_path)
    assert not set(_exposure_for(services, record, None).activated) & HANDBOOK_TOOLS
    services.skills.activate("rinari-handbook", record.id)
    pinned = services.ctx.session_repo.get(record.id)
    assert set(_exposure_for(services, pinned, None).activated) >= HANDBOOK_TOOLS
    # A pinned skill that no longer exists is skipped, not an error.
    ghost = replace(pinned, active_skills=(("gone-skill", "1.0.0"),))
    assert _exposure_for(services, ghost, None).activated == {}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("revisa la sesión", "rinari.session"),
        ("por qué terminó el turno", "rinari.turn"),
        ("buscar sesiones", "rinari.sessions"),
    ],
)
def test_capability_search_finds_the_views_in_spanish(query, expected):
    names = [row["name"] for row in search_capabilities(builtin_catalog(), query, limit=5)]
    assert expected in names, names
