"""Presentation-safe serializers and the RuntimeSnapshot builder."""

from __future__ import annotations

from typing import Any

from rinari.application.services import ServiceContainer
from rinari.engine_protocol import protocol
from rinari.storage.records import ProviderRecord, SessionRecord


def session_to_dict(record: SessionRecord) -> dict[str, Any]:
    skills = None
    if record.active_skills is not None:
        skills = [[name, version] for name, version in record.active_skills]
    return {
        "id": record.id,
        "kind": record.kind,
        "title": record.title,
        "project_id": record.project_id,
        "project_root": record.project_root_snapshot,
        "created_cwd": record.created_cwd,
        "current_cwd": record.current_cwd,
        "provider_id": record.provider_id,
        "model_id": record.model_id,
        "profile_id": record.profile_id,
        "mode": record.mode,
        "state": record.state,
        "git_branch": record.git_branch,
        "forked_from": record.forked_from,
        "active_skills": skills,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_active_at": record.last_active_at,
    }


def provider_to_dict(record: ProviderRecord, has_credential: bool) -> dict[str, Any]:
    """Redacted provider view: identity and status only, never secret material."""
    return {
        "id": record.id,
        "alias": record.alias,
        "type": record.type,
        "auth_method": record.auth_method,
        "account_hint": record.account_hint,
        "endpoint": record.endpoint,
        "default_model_id": record.default_model_id,
        "last_used_model_id": record.last_used_model_id,
        "status_connected": record.status_connected,
        "status_checked_at": record.status_checked_at,
        "has_credential": bool(has_credential),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def build_snapshot(services: ServiceContainer) -> dict[str, Any]:
    """Full UI-reconstruction state: stable presentation-safe data only."""
    sessions = [session_to_dict(s) for s in services.sessions.list(limit=200)]
    providers = []
    for provider in services.providers.list():
        credential = services.ctx.provider_repo.get_credential(provider.id)
        providers.append(provider_to_dict(provider, credential is not None))
    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "engine_version": protocol.engine_version(),
        "sessions": sessions,
        "providers": providers,
    }
