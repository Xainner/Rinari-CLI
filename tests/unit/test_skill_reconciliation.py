"""Skill version reconciliation (phase 4).

The skill *runtime* lands in phase 6, but the resume contract is phase 4:
a session records the (name, version) pairs of skills it had active, and
`rinari resume` must warn when one of them is no longer installed or its
version changed on disk. This file pins the catalog (discovery + version
identity), the durable state round-trip, the reconciler subsystem, and the
fork copy.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.reconcile import OK
from rinari.application.services import build_services
from rinari.skills.catalog import discover_skills, parse_frontmatter, skill_version


def _finding(started, subsystem: str):
    return next(f for f in started.findings if f.subsystem == subsystem)


def write_skill(base: Path, name: str, version: str | None, description: str = "desc") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n"
    if version is not None:
        text += f"version: {version}\n"
    text += "---\n# body\n"
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir / "SKILL.md"


@pytest.fixture
def env(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    s.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    s.models.add("fake", "fake-model-1", "fake-one")
    s.providers.use("fake")
    return app_ctx, s


# -- catalog ---------------------------------------------------------------


def test_parse_frontmatter_fields() -> None:
    raw = '---\nname: "demo"\ndescription: it works\nversion: 1.2.3\n---\nbody\n'
    fields = parse_frontmatter(raw)
    assert fields == {"name": "demo", "description": "it works", "version": "1.2.3"}


def test_parse_frontmatter_absent_or_nested() -> None:
    assert parse_frontmatter("no frontmatter here\n") == {}
    # only top-level `key: value` lines; indented (nested) lines are skipped
    fields = parse_frontmatter("---\nname: demo\n  nested: nope\n---\n")
    assert fields == {"name": "demo"}


def test_skill_version_declared_wins() -> None:
    text = "---\nname: demo\nversion: 2.0\n---\nbody\n"
    assert skill_version(text) == "2.0"


def test_skill_version_sha_fallback_is_content_bound() -> None:
    base = "---\nname: demo\n---\nbody v1\n"
    first = skill_version(base)
    assert first.startswith("sha:")
    assert skill_version(base) == first  # deterministic
    assert skill_version("---\nname: demo\n---\nbody v2\n") != first


def test_discover_shadows_global_with_project(tmp_path) -> None:
    g = tmp_path / "global"
    p = tmp_path / "project"
    write_skill(g, "shared", "1.0.0")
    write_skill(g, "global-only", "1.0.0")
    write_skill(p, "shared", "9.9.9")
    catalog = discover_skills(str(g), str(p))
    assert set(catalog) == {"shared", "global-only"}
    assert catalog["shared"].version == "9.9.9"
    assert catalog["shared"].source == "project"
    assert catalog["global-only"].source == "global"


def test_discover_missing_dirs_is_empty(tmp_path) -> None:
    assert discover_skills(str(tmp_path / "nope"), None) == {}


# -- durable state -----------------------------------------------------------


def test_active_skills_round_trip(env, tmp_path):
    s = env[1]
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    updated = replace(record, active_skills=(("demo", "1.0.0"), ("other", "sha:abcd1234ef56")))
    s.ctx.session_repo.update(updated)
    loaded = s.ctx.session_repo.get(record.id)
    assert loaded.active_skills == (("demo", "1.0.0"), ("other", "sha:abcd1234ef56"))


# -- resume reconciliation ----------------------------------------------------


def test_resume_skills_ok_when_versions_match(env, tmp_path):
    ctx, s = env
    global_dir = ctx.layout.dir("skills")
    write_skill(global_dir, "demo", "1.0.0")
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    s.ctx.session_repo.update(replace(record, active_skills=(("demo", "1.0.0"),)))
    started = s.sessions.resume(record.id)
    assert _finding(started, "skills").state == OK
    assert started.warnings == ()


def test_resume_warns_when_skill_uninstalled(env, tmp_path):
    global_dir = env[0].layout.dir("skills")
    s = env[1]
    skill_file = write_skill(global_dir, "demo", "1.0.0")
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    s.ctx.session_repo.update(replace(record, active_skills=(("demo", "1.0.0"),)))
    assert _finding(s.sessions.resume(record.id), "skills").state == OK

    shutil.rmtree(skill_file.parent)
    finding = _finding(s.sessions.resume(record.id), "skills")
    assert finding.state == "missing"
    assert "demo" in finding.detail
    assert any("skills" in w for w in s.sessions.resume(record.id).warnings)


def test_resume_warns_when_skill_version_changed(env, tmp_path):
    global_dir = env[0].layout.dir("skills")
    s = env[1]
    write_skill(global_dir, "demo", "1.0.0")
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    s.ctx.session_repo.update(replace(record, active_skills=(("demo", "1.0.0"),)))
    assert _finding(s.sessions.resume(record.id), "skills").state == OK

    write_skill(global_dir, "demo", "1.1.0")
    finding = _finding(s.sessions.resume(record.id), "skills")
    assert finding.state == "changed"
    assert "1.0.0 -> 1.1.0" in finding.detail


def test_resume_uses_project_skills_for_project_sessions(env, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    write_skill(root / ".rinari" / "skills", "proj-skill", "3.2.1")
    s = env[1]
    record = s.sessions.start(root).session
    s.trust.add(root)
    s.ctx.session_repo.update(replace(record, active_skills=(("proj-skill", "3.2.1"),)))
    finding = _finding(s.sessions.resume(record.id), "skills")
    assert finding.state == OK, finding.detail


def test_resume_no_active_skills_is_ok(env, tmp_path):
    s = env[1]
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    finding = _finding(s.sessions.resume(record.id), "skills")
    assert finding.state == OK
    assert "no active skills" in finding.detail


# -- fork ----------------------------------------------------------------------


def test_fork_copies_active_skills(env, tmp_path):
    global_dir = env[0].layout.dir("skills")
    s = env[1]
    write_skill(global_dir, "demo", "1.0.0")
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    s.ctx.session_repo.update(replace(record, active_skills=(("demo", "1.0.0"),)))
    started = s.sessions.fork(record.id)
    assert started.session.forked_from == record.id
    assert started.session.active_skills == (("demo", "1.0.0"),)
    # source is untouched
    assert s.ctx.session_repo.get(record.id).active_skills == (("demo", "1.0.0"),)


def test_resume_recognizes_packaged_active_skill(env, tmp_path):
    _, services = env
    cwd = tmp_path / "chat"
    cwd.mkdir()
    record = services.sessions.start(cwd).session
    services.skills.activate("implement-feature", record.id)
    for _ in range(2):
        resumed = services.sessions.resume(record.id)
        assert _finding(resumed, "skills").state == OK
        assert not any("[skills]" in warning for warning in resumed.warnings)
