"""Skill runtime tests (phase 6): manifest, discovery, activation, lifecycle, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari.application.services import build_services
from rinari.cli.main import app
from rinari.skills.manifest import (
    SkillError,
    load_skill_manifest,
    parse_frontmatter_lists,
    validate_skill,
)

SAMPLE = """---
name: sample-skill
description: A sample procedural skill.
version: 1.2.3
risk: medium
can_delegate: true
required_tools:
  - fs.read
  - shell.exec
optional_tools:
  - git.log
triggers:
  - example trigger
---

# Procedure
1. Do the thing.
2. Check the evidence.

# Verification
- The thing is done and evidenced.

# Failure handling
- Stop and report when blocked.

# Success criteria
- Verified, evidenced, no scope creep.
"""


def _write_skill(tmp_path: Path, name: str, content: str = SAMPLE) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def test_frontmatter_lists_and_scalars():
    fields = parse_frontmatter_lists(SAMPLE)
    assert fields["name"] == "sample-skill"
    assert fields["version"] == "1.2.3"
    assert fields["required_tools"] == ["fs.read", "shell.exec"]
    assert fields["optional_tools"] == ["git.log"]
    assert fields["triggers"] == ["example trigger"]
    assert fields["can_delegate"] == "true"


def test_load_manifest_sections_and_body():
    root = Path(__file__).resolve().parents[2] / "src" / "rinari" / "assets" / "skills" / "fix-ci"
    m = load_skill_manifest(root, "packaged")
    assert m.name == "fix-ci"
    assert m.version == "1.0.0"
    assert m.procedure.startswith("1. Read the failure list")
    assert m.verification
    assert m.failure_policy
    assert m.success_criteria
    assert "name: fix-ci" not in m.body  # frontmatter never bleeds into the body
    assert (
        m.requested_tools()
        if hasattr(m, "requested_tools")
        else (m.required_tools + m.optional_tools)
    )


def test_validate_flags_unknown_tool_and_missing_sections():
    from rinari.skills.manifest import SkillManifest

    bad = SkillManifest(
        name="bad",
        description="",
        version="1.0.0",
        source="user",
        required_tools=("nope.tool",),
    )
    issues = validate_skill(bad, {"fs.read"})
    codes = [i["code"] for i in issues]
    assert "MISSING_DESCRIPTION" in codes
    assert "MISSING_PROCEDURE" in codes
    assert "TOOL_NOT_FOUND" in codes


# ---------------------------------------------------------------------------
# Discovery (conflict order + trust gate)
# ---------------------------------------------------------------------------


def test_discovery_packaged_present(app_ctx):
    services = build_services(app_ctx)
    found = services.skills.discover(None)
    assert "fix-ci" in found
    assert found["fix-ci"].source == "packaged"


def test_discovery_user_overrides_packaged(app_ctx, tmp_path):
    services = build_services(app_ctx)
    user_dir = services.skills.user_skills_dir()
    _write_skill(user_dir, "fix-ci", SAMPLE.replace("name: sample-skill", "name: fix-ci"))
    found = services.skills.discover(None)
    assert found["fix-ci"].source == "user"
    assert found["fix-ci"].version == "1.2.3"


def test_discovery_project_overrides_user_and_requires_trust(app_ctx, tmp_path):
    services = build_services(app_ctx)
    root = tmp_path / "proj"
    (root / ".rinari" / "skills").mkdir(parents=True)
    _write_skill(
        root / ".rinari" / "skills",
        "fix-ci",
        SAMPLE.replace("name: sample-skill", "name: fix-ci"),
    )
    # Untrusted: project skills stay invisible.
    found = services.skills.discover(root)
    assert found["fix-ci"].source == "packaged"
    services.trust.add(root)
    found = services.skills.discover(root)
    assert found["fix-ci"].source == "project"


# ---------------------------------------------------------------------------
# Activation: session state + trace
# ---------------------------------------------------------------------------


def _service_session(app_ctx, tmp_path):
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
    record = services.sessions.start(cwd, forced_chat=True).session
    return services, record


def test_activate_deactivate_persist_and_trace(app_ctx, tmp_path):
    services, record = _service_session(app_ctx, tmp_path)
    m = services.skills.activate("fix-ci", record.id)
    assert m.version
    events = [e for e in services.ctx.event_repo.list(record.id) if e.type == "SkillActivated"]
    assert len(events) == 1

    reloaded = services.ctx.session_repo.get(record.id)
    assert ("fix-ci", m.version) in reloaded.active_skills

    removed = services.skills.deactivate("fix-ci", record.id)
    assert removed is True
    reloaded = services.ctx.session_repo.get(record.id)
    # Empty pin list round-trips as NULL (repo stores None when no skills).
    assert all(name != "fix-ci" for name, _ in (reloaded.active_skills or ()))


def test_summaries_marks_active(app_ctx, tmp_path):
    services, record = _service_session(app_ctx, tmp_path)
    services.skills.activate("fix-ci", record.id)
    record = services.ctx.session_repo.get(record.id)  # reload: activate persists a copy
    rows = services.skills.summaries(None, session=record)
    row = next(r for r in rows if r["name"] == "fix-ci")
    assert row["active"] is True


def test_activate_unknown_skill_raises(app_ctx, tmp_path):
    services, record = _service_session(app_ctx, tmp_path)
    with pytest.raises(SkillError) as exc:
        services.skills.activate("does-not-exist", record.id)
    assert exc.value.code == "SKILL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Search / lazy load
# ---------------------------------------------------------------------------


def test_search_ranks_matching_first(app_ctx):
    services = build_services(app_ctx)
    rows = services.skills.search("ci pipeline")
    assert rows[0]["name"] == "fix-ci"


def test_lazy_load_returns_body(app_ctx):
    services = build_services(app_ctx)
    body = services.skills.load("fix-ci")
    assert "name: fix-ci" not in body
    assert "# Procedure" in body


# ---------------------------------------------------------------------------
# User lifecycle: install / update / remove / create
# ---------------------------------------------------------------------------


def test_install_update_remove(app_ctx, tmp_path):
    services = build_services(app_ctx)
    src = _write_skill(tmp_path, "my-skill")
    services.skills.install(src)
    assert (services.skills.user_skills_dir() / "my-skill" / "SKILL.md").is_file()

    # Reinstall without --name collides on the discovered name.
    with pytest.raises(SkillError) as exc:
        services.skills.install(src)
    assert exc.value.code == "ALREADY_EXISTS"

    src2 = _write_skill(
        tmp_path,
        "my-skill-v2",
        SAMPLE.replace("version: 1.2.3", "version: 2.0.0"),
    )
    m2 = services.skills.update(src2, "my-skill")
    assert m2.version == "2.0.0"

    assert services.skills.remove("my-skill") is True
    assert services.skills.remove("my-skill") is False


def test_install_requires_skill_md(app_ctx, tmp_path):
    services = build_services(app_ctx)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillError) as exc:
        services.skills.install(empty)
    assert exc.value.code == "SKILL_NOT_FOUND"


def test_create_scaffolds_valid_skill(app_ctx):
    services = build_services(app_ctx)
    path = services.skills.create("new-skill", "A brand new skill.")
    assert Path(path).is_file()
    report = services.skills.validate("new-skill")
    assert report[0]["ok"] is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(tmp_path, monkeypatch, *args):
    monkeypatch.setenv("RINARI_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    return runner.invoke(app, list(args))


def test_cli_list_show_validate_test(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch, "skills", "list")
    assert res.exit_code == 0
    assert "fix-ci" in res.output
    res = _cli(tmp_path, monkeypatch, "skills", "show", "fix-ci")
    assert res.exit_code == 0
    assert "# Procedure" in res.output
    assert "name: fix-ci" not in res.output.split("# Procedure")[0].split("optional:")[1]
    res = _cli(tmp_path, monkeypatch, "skills", "validate")
    assert res.exit_code == 0
    assert "skills valid" in res.output
    res = _cli(tmp_path, monkeypatch, "skills", "test", "fix-ci")
    assert res.exit_code == 0
    assert "OK" in res.output
    res = _cli(tmp_path, monkeypatch, "skills", "path", "fix-ci")
    assert res.exit_code == 0
    out = res.output.strip()
    assert out.endswith("SKILL.md")
    assert "fix-ci" in out


def test_cli_create_install_remove(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch, "skills", "create", "cli-skill", "-d", "Demo skill.")
    assert res.exit_code == 0
    assert "created skill template" in res.output
    res = _cli(tmp_path, monkeypatch, "skills", "validate", "cli-skill")
    assert "1/1 skills valid" in res.output
    res = _cli(tmp_path, monkeypatch, "skills", "remove", "cli-skill")
    assert res.exit_code == 0
    assert "removed cli-skill" in res.output


def test_cli_activate_deactivate_last_session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("RINARI_HOME", str(home))
    from rinari.application.context import build_app_context
    from rinari.application.provider_service import AddProviderInput
    from rinari.shared.clock import FakeClock

    ctx = build_app_context(home=str(home), clock=FakeClock(start=1_700_000_000.0, step=1.0))
    try:
        services = build_services(ctx)
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
        record = services.sessions.start(cwd, forced_chat=True).session
    finally:
        ctx.close()

    res = _cli(tmp_path, monkeypatch, "skills", "activate", "fix-ci", "--session", record.id)
    assert res.exit_code == 0
    assert f"on session {record.id}" in res.output

    res = _cli(tmp_path, monkeypatch, "skills", "deactivate", "fix-ci", "--session", record.id)
    assert res.exit_code == 0
    assert "deactivated fix-ci" in res.output
