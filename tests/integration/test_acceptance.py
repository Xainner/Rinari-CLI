"""Integration acceptance tests (commands.md sections 81-84).

Full AppContext + real SQLite store in a temporary Rinari home; no network
calls. Also covers the permanent safety invariants from AGENTS.md section 27.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import ServiceContainer, build_services
from rinari.shared.clock import FakeClock
from rinari.shared.errors import PermissionDeniedError
from rinari.shared.paths import ENV_HOME


@dataclass(frozen=True)
class _Env:
    svc: ServiceContainer
    user_home: Path
    empty_dir: Path
    repo_dir: Path


@pytest.fixture
def env(tmp_path, monkeypatch) -> _Env:
    rinari_home = tmp_path / "rinari-home"
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    monkeypatch.setenv(ENV_HOME, str(rinari_home))
    ctx = build_app_context(home=str(rinari_home), clock=FakeClock(start=1_700_000_000.0, step=1.0))
    yield _Env(
        svc=build_services(ctx, user_home=user_home),
        user_home=user_home,
        empty_dir=empty_dir,
        repo_dir=repo_dir,
    )
    ctx.close()


def _add(s: ServiceContainer, alias: str, secret: str = "sk-test-123456") -> None:
    s.providers.add(
        AddProviderInput(
            alias=alias,
            provider_type="openai",
            auth_method="api-key",
            secret=secret,
        )
    )


def _configure(s: ServiceContainer, alias: str = "a") -> None:
    """Minimal usable configuration: one provider with one active model."""
    _add(s, alias)
    s.models.add(alias, "model-a1", "a1")
    s.providers.use(alias)


# -- section 81: provider switching ------------------------------------------


def test_provider_switch_preserves_both_providers(env):
    s = env.svc
    _add(s, "a")
    _add(s, "b")
    m_a1 = s.models.add("a", "model-a1", "a1")
    m_b1 = s.models.add("b", "model-b1", "b1")

    s.providers.use("a")
    s.providers.use("b")
    selection = s.providers.use("a")

    assert {p.alias for p in s.providers.list()} == {"a", "b"}
    for alias in ("a", "b"):
        ref = s.providers.credential_ref(s.providers.get(alias))
        assert ref is not None
        assert s.credentials.exists(ref)
    assert selection.provider.alias == "a"
    assert selection.model is not None and selection.model.id == m_a1.id
    assert s.providers.get("b").default_model_id == m_b1.id


# -- section 82: model switching ---------------------------------------------


def test_model_switch_preserves_both_models(env):
    s = env.svc
    _add(s, "a")
    m1 = s.models.add("a", "model-a1", "a1")
    m2 = s.models.add("a", "model-a2", "a2")

    s.models.use("a2")
    resolved = s.models.use("a1")

    assert {m.id for m in s.models.list("a")} == {m1.id, m2.id}
    assert resolved.model.id == m1.id
    assert resolved.model.settings == m1.settings
    assert s.providers.get("a").default_model_id == m1.id


# -- section 83: logout -------------------------------------------------------


def test_logout_keeps_provider_and_models(env):
    s = env.svc
    _add(s, "openai-personal")
    s.models.add("openai-personal", "model-main", "main")
    s.models.add("openai-personal", "model-fast", "fast")
    model_ids = [m.id for m in s.models.list("openai-personal")]

    record = s.providers.logout("openai-personal")

    assert record.status_connected is False
    provider = s.providers.get("openai-personal")
    assert provider.id == record.id
    assert provider.type == "openai"
    assert [m.id for m in s.models.list("openai-personal")] == model_ids
    assert s.providers.credential_ref(provider) is None


# -- section 84: remove --------------------------------------------------------


def test_remove_only_removes_target(env):
    s = env.svc
    _add(s, "a")
    _add(s, "b")
    s.models.add("a", "model-a1", "a1")
    s.models.add("b", "model-b1", "b1")
    s.providers.use("a")

    s.providers.remove("b")

    assert {p.alias for p in s.providers.list()} == {"a"}
    assert current_alias(s) == "a"
    provider_a = s.providers.get("a")
    assert provider_a.default_model_id is not None
    active = s.providers.current()
    assert active.model is not None and active.model.alias == "a1"


def test_remove_keeps_historical_sessions_readable(env):
    s = env.svc
    _add(s, "a")
    _add(s, "b")
    s.models.add("a", "model-a1", "a1")
    b_id = s.providers.get("b").id
    s.models.add("b", "model-b1", "b1")

    s.providers.use("b")
    started = s.sessions.start(cwd=env.empty_dir)
    session_id = started.session.id
    assert started.session.provider_id == b_id

    s.providers.use("a")
    s.providers.remove("b")

    session = s.sessions.show(session_id)
    assert session.id == session_id
    assert session.provider_id == b_id


def current_alias(s: ServiceContainer) -> str:
    selection = s.providers.current()
    assert selection is not None
    return selection.provider.alias


# -- safety invariants (AGENTS.md section 27) ---------------------------------


def test_home_is_never_implicit_workspace(env):
    s = env.svc
    assert s.sessions.detect(env.user_home).project_root is None
    _configure(s)
    assert s.sessions.start(cwd=env.user_home).session.kind == "CHAT"


def test_arbitrary_dir_without_marker_is_chat(env):
    s = env.svc
    assert s.sessions.detect(env.empty_dir).project_root is None
    _configure(s)
    assert s.sessions.start(cwd=env.empty_dir).session.kind == "CHAT"


def test_chat_forced_inside_repo_stays_chat(env):
    s = env.svc
    assert s.sessions.detect(env.repo_dir).project_root is not None
    _configure(s)
    started = s.sessions.start(cwd=env.repo_dir, forced_chat=True)
    assert started.session.kind == "CHAT"


def test_promote_to_home_is_rejected(env):
    s = env.svc
    _configure(s)
    started = s.sessions.start(cwd=env.empty_dir)
    with pytest.raises(PermissionDeniedError):
        s.sessions.promote(started.session.id, env.user_home)


def test_init_promotes_chat_session_preserving_id(env):
    s = env.svc
    _configure(s)

    project_dir = env.user_home / "newproj"
    started = s.sessions.start(cwd=project_dir)
    session_id = started.session.id
    assert started.session.kind == "CHAT"

    s.projects.init(project_dir, user_home=env.user_home)
    assert s.sessions.detect(project_dir).project_root is not None

    promoted = s.sessions.promote(session_id, project_dir)
    assert promoted.id == session_id
    assert promoted.kind == "PROJECT"
    assert promoted.project_root_snapshot == str(project_dir.resolve())
    assert promoted.provider_id == started.session.provider_id
