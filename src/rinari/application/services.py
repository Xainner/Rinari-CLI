"""Service container for CLI commands (application-level wiring)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.application.model_service import ModelService
from rinari.application.project_service import ProjectService
from rinari.application.provider_service import ProviderService
from rinari.application.session_service import SessionService
from rinari.trust import TrustService


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    ctx: AppContext
    credentials: CredentialStore
    providers: ProviderService
    models: ModelService
    projects: ProjectService
    trust: TrustService
    sessions: SessionService


def build_services(
    ctx: AppContext,
    http_client: httpx.Client | None = None,
    user_home: Path | None = None,
) -> ServiceContainer:
    credentials = CredentialStore(ctx.layout)
    providers = ProviderService(ctx, http_client=http_client, credentials=credentials)
    models = ModelService(ctx, providers, http_client=http_client)
    projects = ProjectService(ctx)
    trust = TrustService(ctx)
    sessions = SessionService(ctx, providers, projects, trust=trust, user_home=user_home)
    return ServiceContainer(
        ctx=ctx,
        credentials=credentials,
        providers=providers,
        models=models,
        projects=projects,
        trust=trust,
        sessions=sessions,
    )
