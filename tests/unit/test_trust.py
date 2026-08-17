"""Project trust store, CLI-level service, and untrusted restrictions (phase 3).

Regression contract:
- trust is granted per canonical path and fingerprinted at grant time;
- a fingerprint change (git HEAD/remote moved) demands revalidation;
- untrusted projects do NOT get their RINARI.md/AGENTS.md injected into the
  prompt, and session start warns the user explicitly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_assembler_context
from rinari.trust import STATE_NOT_FOUND, STATE_NOT_TRUSTED, STATE_REVALIDATION, STATE_TRUSTED


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def services(app_ctx, tmp_path):
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
    return s


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def test_trust_lifecycle(services, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    trust = services.trust

    status = trust.status(repo)
    assert status.state == STATE_NOT_TRUSTED
    assert not trust.is_trusted(repo)

    trust.add(repo)
    assert trust.status(repo).state == STATE_TRUSTED
    assert trust.is_trusted(repo)

    # The store is canonical-path keyed: a relative path resolves to the same entry.
    cwd = repo
    assert trust.status(cwd / ".").state == STATE_TRUSTED

    assert len(trust.list()) == 1
    assert trust.remove(repo) is True
    assert trust.status(repo).state == STATE_NOT_TRUSTED
    assert trust.remove(repo) is False


def test_trust_revalidation_on_identity_change(services, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    services.trust.add(repo)
    assert services.trust.status(repo).state == STATE_TRUSTED

    # New commit moves the identity fingerprint.
    (repo / "b.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "change")
    assert services.trust.status(repo).state == STATE_REVALIDATION
    assert not services.trust.is_trusted(repo)

    # Explicit re-grant restores trust at the new fingerprint.
    services.trust.add(repo)
    assert services.trust.status(repo).state == STATE_TRUSTED


def test_trust_not_found_when_path_gone(services, tmp_path) -> None:
    # Plain (non-git) directory: removable without Windows file locks.
    marker = tmp_path / "plain-project"
    marker.mkdir()
    (marker / ".rinari").mkdir()
    (marker / ".rinari" / "project.toml").write_text("name = 'plain'\n", encoding="utf-8")
    services.trust.add(marker)
    (marker / ".rinari" / "project.toml").unlink()
    (marker / ".rinari").rmdir()
    marker.rmdir()
    assert services.trust.status(marker).state == STATE_NOT_FOUND
    assert not services.trust.is_trusted(marker)


def test_add_requires_existing_path(services, tmp_path) -> None:
    from rinari.shared.errors import InvalidUsageError

    with pytest.raises(InvalidUsageError):
        services.trust.add(tmp_path / "nope-does-not-exist")


def test_untrusted_project_instructions_are_withheld(services, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "RINARI.md").write_text("# Secret project rule: do X\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("agents rule\n", encoding="utf-8")
    record = services.sessions.start(repo).session

    # Untrusted: no project instructions segment, but the environment flags it.
    ctx = build_assembler_context(services, record)
    assert ctx.project_instructions == ()
    assert ctx.environment["project_trust"] == STATE_NOT_TRUSTED
    assert "not trusted" in ctx.environment["project_trust_note"]

    # Session start warned the user:
    started = services.sessions.start(repo)
    assert any("not trusted" in w for w in started.warnings)

    # After an explicit grant the instructions load:
    services.trust.add(repo)
    trusted = services.sessions.start(repo).session
    ctx_trusted = build_assembler_context(services, trusted)
    assert {i.provenance for i in ctx_trusted.project_instructions} == {
        f"{repo.name}/RINARI.md",
        f"{repo.name}/AGENTS.md",
    }
    assert ctx_trusted.environment["project_trust"] == STATE_TRUSTED


def test_revalidation_warns_on_resume(services, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    record = services.sessions.start(repo).session
    services.trust.add(repo)

    (repo / "b.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "move")

    started = services.sessions.resume(record.id, cwd=repo)
    assert any("revalidation" in w for w in started.warnings)
