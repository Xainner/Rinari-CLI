"""Service container for CLI commands (application-level wiring)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from rinari.agents.config import AgentConfigStore
from rinari.agents.registry import AgentRegistry
from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.application.model_service import ModelService
from rinari.application.network_service import NetworkService
from rinari.application.project_service import ProjectService
from rinari.application.provider_service import ProviderService
from rinari.application.session_service import SessionService
from rinari.artifacts.store import ArtifactStore
from rinari.checkpoints.service import CheckpointService
from rinari.context.retrieval import ContextRetrievalService
from rinari.context.service import ContextService
from rinari.hooks import HookService
from rinari.mcp import McpService
from rinari.memory import MemoryService
from rinari.openapi import ApiService
from rinari.plugins import PluginService
from rinari.repo.index_service import IndexService
from rinari.skills.service import SkillService
from rinari.tasks import TaskService
from rinari.trust import TrustService
from rinari.verify.service import VerificationService


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    ctx: AppContext
    credentials: CredentialStore
    providers: ProviderService
    models: ModelService
    projects: ProjectService
    trust: TrustService
    index: IndexService
    tasks: TaskService
    verification: VerificationService
    checkpoints: CheckpointService
    sessions: SessionService
    artifacts: ArtifactStore
    context: ContextService
    memory: MemoryService
    retrieval: ContextRetrievalService
    network: NetworkService
    plugins: PluginService
    mcp: McpService
    api: ApiService
    hooks: HookService
    skills: SkillService
    agents: AgentRegistry
    agent_configs: AgentConfigStore


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
    index = IndexService(ctx)
    tasks = TaskService(ctx)
    verification = VerificationService(ctx)
    checkpoints = CheckpointService(ctx)
    sessions = SessionService(ctx, providers, projects, trust=trust, user_home=user_home)
    artifacts = ArtifactStore(ctx)
    context_service = ContextService(ctx, artifacts)
    memory = MemoryService(ctx)
    retrieval = ContextRetrievalService(ctx, artifacts=artifacts, memory=memory)
    network = NetworkService(ctx)
    plugins = PluginService(ctx, trust)
    mcp = McpService(ctx, trust)
    api = ApiService(ctx, trust)
    hooks = HookService(ctx, trust)
    skills = SkillService(ctx, trust)
    agents = AgentRegistry(trust)
    agent_configs = AgentConfigStore(ctx.layout.root)
    return ServiceContainer(
        ctx=ctx,
        credentials=credentials,
        providers=providers,
        models=models,
        projects=projects,
        trust=trust,
        index=index,
        tasks=tasks,
        verification=verification,
        checkpoints=checkpoints,
        sessions=sessions,
        artifacts=artifacts,
        context=context_service,
        memory=memory,
        retrieval=retrieval,
        network=network,
        plugins=plugins,
        mcp=mcp,
        api=api,
        hooks=hooks,
        skills=skills,
        agents=agents,
        agent_configs=agent_configs,
    )
