"""JSON-safe representations of storage records for `--json` output."""

from __future__ import annotations

from typing import Any

from rinari.providers.adapters.base import DiscoveredModel
from rinari.storage.records import ModelRecord, ProviderRecord, SessionRecord


def provider_dict(
    record: ProviderRecord, active: bool = False, credential_ref: str | None = None
) -> dict[str, Any]:
    return {
        "id": record.id,
        "alias": record.alias,
        "type": record.type,
        "auth_method": record.auth_method,
        "account_hint": record.account_hint,
        "endpoint": record.endpoint,
        "settings": record.settings,
        "status_connected": record.status_connected,
        "status_checked_at": record.status_checked_at,
        "default_model_id": record.default_model_id,
        "last_used_model_id": record.last_used_model_id,
        "active": active,
        "credential_ref": credential_ref,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def model_dict(
    model: ModelRecord, provider_alias: str | None = None, active: bool = False
) -> dict[str, Any]:
    return {
        "id": model.id,
        "alias": model.alias,
        "provider_id": model.provider_id,
        "provider": provider_alias,
        "provider_model_id": model.provider_model_id,
        "capabilities": model.capabilities,
        "availability": model.availability,
        "settings": model.settings,
        "active": active,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def session_dict(record: SessionRecord) -> dict[str, Any]:
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
        "profile": record.profile_id,
        "mode": record.mode,
        "state": record.state,
        "git_branch": record.git_branch,
        "forked_from": record.forked_from,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_active_at": record.last_active_at,
    }


def discovered_model_dict(model: DiscoveredModel) -> dict[str, Any]:
    return {
        "provider_model_id": model.provider_model_id,
        "capabilities": model.capabilities,
        "availability": model.availability,
    }
