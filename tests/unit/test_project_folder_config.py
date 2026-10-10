"""A project's trusted extra folders bring their RINARI.md and hooks, each scoped."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_assembler_context, folder_instructions
from rinari.engine_protocol.server import EngineServer
from rinari.hooks.engine import HookEngine


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    container = build_services(app_ctx, user_home=home)
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


def _folder(tmp_path: Path, name: str, rinari_md: str | None = None) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    if rinari_md is not None:
        (path / "RINARI.md").write_text(rinari_md, encoding="utf-8")
    return path


def _hooks(folder: Path, *names: str) -> None:
    (folder / ".rinari").mkdir(exist_ok=True)
    (folder / ".rinari" / "hooks.json").write_text(
        json.dumps(
            [
                {
                    "name": name,
                    "event": "SessionStart",
                    "handler_type": "python",
                    "handler": "json:dumps",
                }
                for name in names
            ]
        ),
        encoding="utf-8",
    )


# -- RINARI.md ---------------------------------------------------------------------------


def test_trusted_extra_folders_bring_their_instructions_labelled(services, tmp_path):
    api = _folder(tmp_path, "api", "Use FastAPI.")
    web = _folder(tmp_path, "web", "Use pnpm, never npm.")
    docs = _folder(tmp_path, "docs", "SECRET-UNTRUSTED")
    services.projects.create(name="Tienda", folders=[str(api), str(web), str(docs)])
    services.trust.add(api)
    services.trust.add(web)
    record = services.sessions.new(api, mode="build")

    context = build_assembler_context(services, record)

    by_source = {i.provenance: i.content for i in context.project_instructions}
    assert by_source["./RINARI.md"] == "Use FastAPI."
    web_entry = by_source[f"folder {web.resolve()}: ./RINARI.md"]
    assert web_entry.startswith(f"These instructions apply to work inside {web.resolve()}.")
    assert web_entry.endswith("Use pnpm, never npm.")
    # An untrusted folder stays data: nothing of it reaches the prompt.
    assert not any("SECRET-UNTRUSTED" in i.content for i in context.project_instructions)


def test_extra_folder_instructions_follow_the_cwd_inside_it(tmp_path):
    web = _folder(tmp_path, "web", "Root rules.")
    ui = _folder(web, "ui", "UI rules.")
    inside = folder_instructions((web.resolve(),), ui)
    assert [i.provenance for i in inside] == [
        f"folder {web.resolve()}: ./RINARI.md",
        f"folder {web.resolve()}: ui/RINARI.md",
    ]
    # Working elsewhere, only the folder's own file applies.
    elsewhere = folder_instructions((web.resolve(),), tmp_path)
    assert [i.provenance for i in elsewhere] == [f"folder {web.resolve()}: ./RINARI.md"]


def test_an_extra_folder_counts_even_when_the_primary_is_untrusted(services, tmp_path):
    api = _folder(tmp_path, "api", "Primary rules.")
    web = _folder(tmp_path, "web", "Web rules.")
    services.projects.create(name="Tienda", folders=[str(api), str(web)])
    services.trust.add(web)
    record = services.sessions.new(api, mode="build")

    context = build_assembler_context(services, record)

    contents = [i.content for i in context.project_instructions]
    assert not any("Primary rules." in c for c in contents)
    assert any(c.endswith("Web rules.") for c in contents)


# -- hooks ----------------------------------------------------------------------------------


def test_extra_folder_hooks_are_discovered_with_their_folder(services, tmp_path):
    api, web = _folder(tmp_path, "api"), _folder(tmp_path, "web")
    _hooks(api, "root-hook")
    _hooks(web, "web-hook")

    rows = services.hooks.list(api.resolve(), folders=(web.resolve(),))

    by_name = {row["name"]: row for row in rows}
    assert "folder" not in by_name["root-hook"]
    assert by_name["web-hook"]["folder"] == str(web.resolve())
    assert by_name["web-hook"]["source"] == "project"


def test_extra_folder_hooks_run_only_while_their_folder_is_trusted(services, tmp_path):
    api, web = _folder(tmp_path, "api"), _folder(tmp_path, "web")
    _hooks(web, "web-hook")
    services.trust.add(api)
    engine = services.hooks.build_engine(project=api.resolve(), folders=(web.resolve(),))

    skipped = engine.emit("SessionStart", {"x": 1}, project=api.resolve())
    assert [(o.name, o.ok, o.error) for o in skipped] == [("web-hook", False, "TRUST_REQUIRED")]

    services.trust.add(web)
    ran = engine.emit("SessionStart", {"x": 1}, project=api.resolve())
    assert [(o.name, o.ok) for o in ran] == [("web-hook", True)]


def test_root_hooks_still_answer_to_the_project_trust(services, tmp_path):
    api = _folder(tmp_path, "api")
    _hooks(api, "root-hook")
    engine = HookEngine(trust=services.trust)
    engine.add_all(services.hooks.discover(api.resolve()))
    assert engine.emit("SessionStart", {}, project=api.resolve())[0].error == "TRUST_REQUIRED"
    services.trust.add(api)
    assert engine.emit("SessionStart", {}, project=api.resolve())[0].ok


# -- protocol --------------------------------------------------------------------------------


def test_project_intelligence_lists_each_folder_scope(services, tmp_path):
    api = _folder(tmp_path, "api", "Primary rules.")
    web = _folder(tmp_path, "web", "Web rules.")
    services.projects.create(name="Tienda", folders=[str(api), str(web)])
    services.trust.add(api)
    services.trust.add(web)
    server = EngineServer(services, user_home=tmp_path / "home")
    try:
        response = server.handle_line(
            json.dumps({"id": "r1", "method": "project.intelligence", "params": {"path": str(api)}})
        )
    finally:
        server.close()
    scopes = response["result"]["instructions"]["scopes"]
    assert [s["provenance"] for s in scopes] == [
        "./RINARI.md",
        f"folder {web.resolve()}: ./RINARI.md",
    ]
    assert scopes[1]["folder"] == str(web.resolve())
