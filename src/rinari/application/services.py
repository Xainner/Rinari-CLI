"""Service container for CLI commands (application-level wiring)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from rinari.agency import AgencyService
from rinari.agency.teams import TeamService
from rinari.agents.config import AgentConfigStore
from rinari.agents.registry import AgentRegistry
from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.application.job_watches import JobWatchService
from rinari.application.model_service import ModelService
from rinari.application.network_service import NetworkService
from rinari.application.project_service import ProjectService
from rinari.application.provider_service import ProviderService
from rinari.application.rinari_profiles import RinariProfileService
from rinari.application.session_service import SessionService
from rinari.artifacts.store import ArtifactStore
from rinari.changes.service import TurnChangeService
from rinari.checklist import ChecklistService
from rinari.checkpoints.service import CheckpointService
from rinari.context.retrieval import ContextRetrievalService
from rinari.context.service import ContextService
from rinari.followups import FollowupService
from rinari.goals import GoalService
from rinari.hooks import HookService
from rinari.mcp import McpService
from rinari.memory import MemoryService
from rinari.openapi import ApiService
from rinari.plugins import PluginService
from rinari.repo.index_service import IndexService
from rinari.resumes import ResumeService
from rinari.schedule.service import ScheduleService
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
    changes: TurnChangeService
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
    schedules: ScheduleService = None
    rinari_profiles: RinariProfileService = None
    checklist: ChecklistService = None
    goals: GoalService = None
    resumes: ResumeService = None
    followups: FollowupService = None
    agency: AgencyService = None
    teams: TeamService = None
    job_watches: JobWatchService = None


def build_services(
    ctx: AppContext,
    http_client: httpx.Client | None = None,
    user_home: Path | None = None,
) -> ServiceContainer:
    from rinari.providers import metadata

    metadata.use_cache_dir(ctx.layout.dir("cache"))
    credentials = CredentialStore(ctx.layout)
    providers = ProviderService(ctx, http_client=http_client, credentials=credentials)
    models = ModelService(ctx, providers, http_client=http_client)
    projects = ProjectService(ctx)
    trust = TrustService(ctx)
    index = IndexService(ctx)
    tasks = TaskService(ctx)
    verification = VerificationService(ctx)
    checkpoints = CheckpointService(ctx)
    changes = TurnChangeService(ctx)
    sessions = SessionService(ctx, providers, projects, trust=trust, user_home=user_home)
    artifacts = ArtifactStore(ctx)
    context_service = ContextService(ctx, artifacts)
    memory = MemoryService(ctx)
    retrieval = ContextRetrievalService(ctx, artifacts=artifacts, memory=memory)
    network = NetworkService(ctx)
    plugins = PluginService(ctx, trust)
    mcp = McpService(ctx, trust, credentials=credentials)
    api = ApiService(ctx, trust)
    hooks = HookService(ctx, trust)
    skills = SkillService(ctx, trust, user_home=user_home)
    agents = AgentRegistry(trust)
    agent_configs = AgentConfigStore(ctx.layout.root)
    schedules = ScheduleService(
        ctx.schedule_repo,
        ctx.clock,
        ctx.ids,
        project_exists=lambda project_id: _exists(projects.get, project_id),
        model_exists=lambda model: _exists(models.resolve, model),
        # A task may name a project skill: its folder is only known at run time.
        skill_exists=lambda name: True,
        # Rinari Agency: a permanent rule belongs to an agent.
        agent_exists=lambda agent_id: ctx.agency_repo.get(agent_id) is not None,
        # «Repetir aquí»: the conversation the task runs in.
        session_exists=lambda session_id: ctx.session_repo.get(session_id) is not None,
    )
    rinari_profiles = RinariProfileService(ctx)
    projects.profiles = rinari_profiles
    sessions.profiles = rinari_profiles
    # A broken profile file never keeps the engine from starting.
    with contextlib.suppress(Exception):
        rinari_profiles.reconcile_orphans()
    agency = AgencyService(
        ctx.agency_repo,
        ctx.clock,
        ctx.ids,
        resolve_model=lambda model: _model_id(models, model),
        connector_exists=lambda name: any(
            row.get("name") == name for row in ctx.mcp_server_repo.list(None)
        ),
        soul_exists=lambda soul_id: _soul_exists(ctx.home, soul_id),
    )
    teams = TeamService(
        ctx.team_repo,
        agency,
        ctx.clock,
        ctx.ids,
        connector_exists=lambda name: any(
            row.get("name") == name for row in ctx.mcp_server_repo.list(None)
        ),
    )
    mcp.scope_of = lambda session_id: _agent_scope(agency, session_id, ctx)
    # An agent's rule may deliver to the office of one of its teams or to a teammate.
    schedules.check_delivery = teams.delivery_target
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
        changes=changes,
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
        schedules=schedules,
        rinari_profiles=rinari_profiles,
        checklist=ChecklistService(ctx),
        job_watches=JobWatchService(mcp),
        goals=GoalService(ctx),
        resumes=ResumeService(ctx),
        followups=FollowupService(ctx),
        agency=agency,
        teams=teams,
    )


def _agent_scope(agency: AgencyService, session_id: str, ctx: AppContext):
    """(allowed connectors, owner tags) of an agent's conversation, else (None, None).

    D-33: a post (A2) sees the agent's own connectors and its team's, and the
    accounts of both (owner agent:ID and team:ID), never another team's: in
    each post, the agent plus that team. Elsewhere, only the agent's.
    """
    agent = agency.for_session(session_id)
    if agent is None:
        return None, None
    own = frozenset(agent.get("connectors") or ())
    tag = f"agent:{agent['id']}"
    record = ctx.session_repo.get(session_id) if ctx.team_repo else None
    team_id = ctx.team_repo.active_team(record) if record is not None else None
    if team_id is not None:
        # A post, or a child of one: also the team's connectors and accounts.
        team = ctx.team_repo.get(team_id)
        if team is not None:
            return own | frozenset(team.get("connectors") or ()), (tag, f"team:{team['id']}")
    return own, tag


def _soul_exists(home, soul_id: str) -> bool:
    from rinari.soul.store import SoulStore

    try:
        SoulStore(home).get(soul_id)
    except Exception:
        return False
    return True


def _model_id(models, ref: str) -> str | None:
    try:
        return models.resolve(ref).id
    except Exception:
        return None


def _exists(lookup, ref: str) -> bool:
    try:
        lookup(ref)
    except Exception:
        return False
    return True
