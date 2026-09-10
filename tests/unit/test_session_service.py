from pathlib import Path

import pytest

from rinari.application.model_service import ModelService
from rinari.application.project_service import ProjectService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.application.session_service import (
    EVENT_SESSION_PROMOTED,
    EVENT_SESSION_STARTED,
    EVENT_USER_PROMPT,
    SessionService,
)
from rinari.shared.errors import (
    AuthenticationRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def services(app_ctx, home):
    providers = ProviderService(app_ctx)
    models = ModelService(app_ctx, providers)
    projects = ProjectService(app_ctx)
    sessions = SessionService(app_ctx, providers, projects, user_home=home)
    return SimpleServices(providers, models, projects, sessions)


class SimpleServices:
    def __init__(self, providers, models, projects, sessions) -> None:
        self.providers = providers
        self.models = models
        self.projects = projects
        self.sessions = sessions


def _configure(services, home: Path) -> None:
    project = home / "code" / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)
    services.providers.add(
        AddProviderInput(alias="openai-personal", provider_type="openai", secret="sk-a")
    )
    services.models.add("openai-personal", "gpt-1", "gpt-main")
    services.providers.use("openai-personal")


def test_start_without_provider_gives_config_required(app_ctx, services, home) -> None:
    cwd = home / "work"
    cwd.mkdir()
    with pytest.raises(AuthenticationRequiredError) as excinfo:
        services.sessions.start(cwd)
    assert "CONFIG_REQUIRED" in excinfo.value.message
    assert "rinari setup" in (excinfo.value.hint or "")


def test_start_plain_directory_creates_chat_session(services, home) -> None:
    _configure(services, home)
    cwd = home / "loose"
    cwd.mkdir()
    started = services.sessions.start(cwd)
    assert started.created is True
    assert started.session.kind == "CHAT"
    assert started.session.project_id is None

    resumed = services.sessions.start(cwd)
    assert resumed.created is False
    assert resumed.session.id == started.session.id


def test_chat_forced_inside_repo_stays_chat(services, home) -> None:
    _configure(services, home)
    started = services.sessions.start(home / "code" / "demo", forced_chat=True)
    assert started.session.kind == "CHAT"
    assert started.session.project_id is None


def test_auto_inside_repo_creates_project_session(services, home) -> None:
    _configure(services, home)
    started = services.sessions.start(home / "code" / "demo")
    assert started.session.kind == "PROJECT"
    assert started.session.project_id is not None
    assert started.session.project_root_snapshot == str(home / "code" / "demo")
    project = services.projects.upsert(home / "code" / "demo")
    assert project.id == started.session.project_id


def test_cwd_inside_home_is_chat(services, home) -> None:
    _configure(services, home)
    (home / ".git").mkdir(exist_ok=True)
    started = services.sessions.start(home)
    assert started.session.kind == "CHAT"


def test_prompt_is_recorded_as_event(services, home) -> None:
    _configure(services, home)
    cwd = home / "loose"
    cwd.mkdir()
    started = services.sessions.start(cwd, prompt="fix the auth tests")
    events = services.sessions._ctx.event_repo.list(started.session.id)
    types = [e.type for e in events]
    assert EVENT_SESSION_STARTED in types
    assert EVENT_USER_PROMPT in types
    prompt_event = next(e for e in events if e.type == EVENT_USER_PROMPT)
    assert prompt_event.payload["prompt"] == "fix the auth tests"


def test_promotion_preserves_identity_and_records_event(services, app_ctx, home) -> None:
    _configure(services, home)
    chat = services.sessions.start(home / "loose")
    started = chat.session

    project_root = home / "code" / "app"
    project_root.mkdir(parents=True)

    promoted = services.sessions.promote(started.id, project_root)

    assert promoted.id == started.id
    assert promoted.kind == "PROJECT"
    assert promoted.provider_id == started.provider_id
    assert promoted.model_id == started.model_id
    assert promoted.project_root_snapshot == str(project_root)

    events = app_ctx.event_repo.list(started.id)
    assert any(e.type == EVENT_SESSION_PROMOTED for e in events)
    promoted_event = next(e for e in events if e.type == EVENT_SESSION_PROMOTED)
    assert promoted_event.payload["previous_kind"] == "CHAT"
    assert promoted_event.payload["project_id"] == promoted.project_id


def test_promotion_of_project_session_conflicts(services, home) -> None:
    _configure(services, home)
    started = services.sessions.start(home / "code" / "demo")
    with pytest.raises(ConflictError):
        services.sessions.promote(started.session.id, home / "code" / "other")


def test_promotion_to_home_rejected(services, home) -> None:
    _configure(services, home)
    started = services.sessions.start(home / "loose")
    with pytest.raises(PermissionDeniedError):
        services.sessions.promote(started.session.id, home)


def test_resume_unknown_session_not_found(services, home) -> None:
    _configure(services, home)
    with pytest.raises(NotFoundError):
        services.sessions.resume("ses_nope")


def test_resume_warns_when_provider_removed(services, app_ctx, home) -> None:
    _configure(services, home)
    started = services.sessions.start(home / "loose")
    session_id = started.session.id
    # remove the provider (models first via service remove path)
    services.models.list()
    for model in list(app_ctx.model_repo.list()):
        app_ctx.model_repo.delete(model.id)
    for provider in list(app_ctx.provider_repo.list()):
        app_ctx.provider_repo.delete(provider.id)

    resumed = services.sessions.resume(session_id, cwd=home / "loose")
    assert any("provider" in w for w in resumed.warnings)
    assert any("model" in w for w in resumed.warnings)


def test_init_and_promote_flow(services, home) -> None:
    """Explicit project creation promotes the most recent CHAT session."""
    _configure(services, home)
    started = services.sessions.start(home / "loose")
    assert started.session.kind == "CHAT"

    project_root = home / "code" / "newapp"
    project_root.mkdir(parents=True)
    record, created_files = services.projects.init(project_root, user_home=home)
    assert any(f.endswith("project.toml") for f in created_files)
    assert record.canonical_root == str(project_root)

    candidate = services.sessions.find_promotable_chat_session(project_root)
    # the session's cwd (loose) is not under newapp, so no auto-promotion
    assert candidate is None

    candidate = services.sessions.find_promotable_chat_session(home / "loose")
    assert candidate is not None
    promoted = services.sessions.promote(candidate.id, project_root)
    assert promoted.id == started.session.id
    assert promoted.kind == "PROJECT"


def test_first_message_title_is_persisted_and_not_replaced(services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    title = "Diseñar un juego de invasión espacial"
    updated = services.sessions.name_from_first_message(record.id, title)
    assert updated.title == title
    assert services.sessions.show(record.id).title == title
    assert services.sessions.name_from_first_message(record.id, "Segundo mensaje").title == title


def test_manual_default_title_is_respected(services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    services.sessions.rename(record.id, "Nueva conversación")
    assert (
        services.sessions.name_from_first_message(record.id, "Otro título").title
        == "Nueva conversación"
    )


def test_first_message_title_is_bounded(services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    title = services.sessions.name_from_first_message(
        record.id, "Diseñar una nave moderna " * 20
    ).title
    assert len(title) <= 72
    assert title.endswith("…")
