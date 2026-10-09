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


def test_generated_title_and_manual_rename_during_generation(services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    result = services.sessions.name_from_first_message(
        record.id,
        "Quiero que arregles el selector",
        title_factory=lambda _: "Mejorar selector de modelos",
    )
    assert result.title == "Mejorar selector de modelos"
    other = services.sessions.new(home, title="New chat", forced_chat=True)

    def concurrent_rename(_):
        services.sessions.rename(other.id, "Mi nombre manual")
        return "Título generado"

    result = services.sessions.name_from_first_message(
        other.id, "Solicitud", title_factory=concurrent_rename
    )
    assert result.title == "Mi nombre manual"


def test_title_generation_failure_keeps_fallback(services, home):
    _configure(services, home)
    record = services.sessions.start(home).session

    def unavailable(_):
        raise RuntimeError("Provider unavailable")

    result = services.sessions.name_from_first_message(
        record.id, "Revisar el proyecto", title_factory=unavailable
    )
    assert result.title == "Revisar el proyecto"


def test_opening_an_active_session_does_not_bump_recency(services, home) -> None:
    """Clicking a conversation to read it must not reorder the list."""
    _configure(services, home)
    cwd = home / "loose"
    cwd.mkdir()
    started = services.sessions.start(cwd)
    before = services.sessions.show(started.session.id).last_active_at

    reopened = services.sessions.resume(ref=started.session.id)

    assert reopened.session.state == "active"
    assert services.sessions.show(started.session.id).last_active_at == before


def test_resuming_an_interrupted_session_does_bump_recency(services, home) -> None:
    """A real resume is activity; only a no-op open is not."""
    _configure(services, home)
    cwd = home / "loose2"
    cwd.mkdir()
    started = services.sessions.start(cwd)
    closed = services.sessions.close(started.session.id)
    before = closed.last_active_at

    services.sessions.resume(ref=started.session.id)

    assert services.sessions.show(started.session.id).last_active_at >= before
    assert services.sessions.show(started.session.id).state == "active"


def _user_message(app_ctx, session_id: str, text: str) -> None:
    from rinari.storage.records import SessionMessageRecord

    app_ctx.message_repo.append_many(
        session_id,
        [
            SessionMessageRecord(
                id=app_ctx.ids.new("msg"), session_id=session_id, seq=0, role="user", content=text
            )
        ],
    )


def _title_events(app_ctx, session_id: str, kind: str) -> list[dict]:
    return [e.payload for e in app_ctx.event_repo.list(session_id) if e.type == kind]


def test_promoted_chat_keeps_an_untouched_default_title(app_ctx, services, home):
    """A CHAT promoted before its first message (opening a folder) still gets named."""
    _configure(services, home)
    folder = home / "RoadRash"
    folder.mkdir()
    record = services.sessions.new(folder, forced_chat=True)
    assert record.title == "chat in RoadRash"
    services.sessions.promote(record.id, folder)
    result = services.sessions.name_from_first_message(
        record.id, "Arregla el salto del personaje", title_factory=lambda _: "Salto del personaje"
    )
    assert result.title == "Salto del personaje"


def test_failed_title_is_provisional_and_retried_from_the_first_message(app_ctx, services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    first = "Recitame un poema en frances sobre porque los huskies son excelentes perros"
    seen: list[str] = []

    def empty(text):
        seen.append(text)
        return ""

    result = services.sessions.name_from_first_message(record.id, first, title_factory=empty)
    assert result.title.startswith("Recitame un poema")
    assert _title_events(app_ctx, record.id, "SessionTitleFailed") == [{"reason": "empty"}]
    assert _title_events(app_ctx, record.id, "SessionRenamed")[-1]["source"] == "fallback"
    _user_message(app_ctx, record.id, first)

    # Next turn: a new message, but the title still summarizes the first one.
    result = services.sessions.name_from_first_message(
        record.id,
        "Ahora en inglés",
        title_factory=lambda text: seen.append(text) or "Poema francés sobre los huskies",
    )
    assert result.title == "Poema francés sobre los huskies"
    assert seen == [first, first]
    assert _title_events(app_ctx, record.id, "SessionRenamed")[-1] == {
        "title": "Poema francés sobre los huskies",
        "source": "generated",
    }
    # Generated titles are final.
    assert (
        services.sessions.name_from_first_message(
            record.id, "Otro", title_factory=lambda _: "No"
        ).title
        == "Poema francés sobre los huskies"
    )


def test_title_retries_are_bounded_and_record_only_the_reason(app_ctx, services, home):
    _configure(services, home)
    record = services.sessions.start(home).session

    def failing(_):
        raise RuntimeError("secret provider detail")

    for _ in range(5):
        services.sessions.name_from_first_message(
            record.id, "Revisar el proyecto", title_factory=failing
        )
        _user_message(app_ctx, record.id, "Revisar el proyecto")
    assert (
        _title_events(app_ctx, record.id, "SessionTitleFailed")
        == [{"reason": "error:RuntimeError"}] * 3
    )
    assert services.sessions.show(record.id).title == "Revisar el proyecto"


def test_a_manual_rename_ends_the_retries(app_ctx, services, home):
    _configure(services, home)
    record = services.sessions.start(home).session
    services.sessions.name_from_first_message(
        record.id, "Revisar el proyecto", title_factory=lambda _: ""
    )
    _user_message(app_ctx, record.id, "Revisar el proyecto")
    services.sessions.rename(record.id, "Mi nombre")
    calls: list[str] = []
    result = services.sessions.name_from_first_message(
        record.id, "Otro", title_factory=lambda text: calls.append(text) or "Generado"
    )
    assert result.title == "Mi nombre"
    assert calls == []


def test_every_rename_is_published_with_its_source(app_ctx, services, home):
    _configure(services, home)
    published: list[dict] = []
    services.sessions.on_renamed = published.append
    record = services.sessions.start(home).session
    services.sessions.name_from_first_message(
        record.id, "Hola", title_factory=lambda _: "Saludo inicial"
    )
    services.sessions.rename(record.id, "A mano")
    assert published == [
        {"session_id": record.id, "title": "Saludo inicial", "source": "generated"},
        {"session_id": record.id, "title": "A mano", "source": "manual"},
    ]


def test_a_greeting_does_not_name_the_session_the_next_topic_does(services, home, app_ctx):
    """53 of 56 real titles were the opening message trimmed, often "Hola"."""
    _configure(services, home)
    record = services.sessions.start(home).session
    default = record.title

    def factory(text: str) -> str:
        return "NONE" if text.strip().lower() in {"hola", "hi"} else "Arreglar el build de la app"

    after_hello = services.sessions.name_from_first_message(
        record.id, "Hola", title_factory=factory
    )
    assert after_hello.title == default
    _user_message(app_ctx, record.id, "Hola")
    named = services.sessions.name_from_first_message(
        record.id, "El build falla con un error de tipos en la app", title_factory=factory
    )
    assert named.title == "Arreglar el build de la app"
    # A session that already had a topic is not renamed again.
    _user_message(app_ctx, record.id, "El build falla con un error de tipos en la app")
    again = services.sessions.name_from_first_message(record.id, "otra cosa", title_factory=factory)
    assert again.title == "Arreglar el build de la app"
