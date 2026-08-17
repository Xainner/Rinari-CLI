"""Application context: the dependency container handed to CLI commands.

Every bootstrap follows the harness boot sequence (harness.md section 6),
trimmed to what Fase 1 needs: home -> database+migrations -> config ->
repositories. Phase 2 will extend this with provider/session resolution
and runtime registries without changing the container shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rinari.application.config.loader import EffectiveConfig, load_effective_config
from rinari.shared.clock import Clock, SystemClock
from rinari.shared.ids import IdGenerator
from rinari.shared.paths import HomeLayout, ensure_layout, resolve_home
from rinari.storage.db import Database
from rinari.storage.migrations import MigrationRunner
from rinari.storage.repositories import (
    ConfigValueRepository,
    ModelRepository,
    ProjectRepository,
    ProviderRepository,
    SessionEventRepository,
    SessionMessageRepository,
    SessionRepository,
    TrustEntryRepository,
    WorktreeBaselineRepository,
)


@dataclass(slots=True)
class AppContext:
    home: Path
    layout: HomeLayout
    db: Database
    clock: Clock
    ids: IdGenerator
    config: EffectiveConfig
    provider_repo: ProviderRepository
    model_repo: ModelRepository
    project_repo: ProjectRepository
    session_repo: SessionRepository
    event_repo: SessionEventRepository
    message_repo: SessionMessageRepository
    worktree_repo: WorktreeBaselineRepository
    trust_repo: TrustEntryRepository
    config_repo: ConfigValueRepository

    def close(self) -> None:
        self.db.close()


def build_app_context(
    home: str | Path | None = None,
    config_path: str | Path | None = None,
    project_config: str | Path | None = None,
    clock: Clock | None = None,
) -> AppContext:
    clock = clock or SystemClock()
    root = resolve_home(home)
    layout = ensure_layout(root)
    db = Database(layout.state_db)
    MigrationRunner(db, clock).migrate()

    user_data: dict | None = None
    if config_path is not None:
        user_data = _load_explicit_user_config(config_path)

    effective_config = load_effective_config(
        layout, project_config=project_config, user_data=user_data
    )

    return AppContext(
        home=root,
        layout=layout,
        db=db,
        clock=clock,
        ids=IdGenerator(clock),
        config=effective_config,
        provider_repo=ProviderRepository(db),
        model_repo=ModelRepository(db),
        project_repo=ProjectRepository(db),
        session_repo=SessionRepository(db),
        event_repo=SessionEventRepository(db),
        message_repo=SessionMessageRepository(db),
        worktree_repo=WorktreeBaselineRepository(db),
        trust_repo=TrustEntryRepository(db),
        config_repo=ConfigValueRepository(db),
    )


def _load_explicit_user_config(config_path: str | Path) -> dict:
    import tomllib

    from rinari.shared.errors import ConfigurationError

    path = Path(config_path).expanduser()
    if not path.is_file():
        raise ConfigurationError(f"Config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Config file {path} is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"Config file {path} must define a TOML table at the root")
    return data
