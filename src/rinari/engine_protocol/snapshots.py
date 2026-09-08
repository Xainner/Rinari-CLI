"""Presentation-safe serializers and the RuntimeSnapshot builder.

Session/model shapes reuse the canonical CLI serializers so the desktop and
the terminal never disagree on field names. Provider views are redacted:
identity and status only, never secret material.
"""

from __future__ import annotations

from typing import Any

from rinari.application.services import ServiceContainer
from rinari.cli.serializers import provider_dict, session_dict
from rinari.engine_protocol import protocol
from rinari.storage.records import ProviderRecord, SessionRecord


def session_to_dict(record: SessionRecord) -> dict[str, Any]:
    return session_dict(record)


def provider_to_dict(record: ProviderRecord, has_credential: bool) -> dict[str, Any]:
    view = provider_dict(record, active=False, credential_ref=None)
    view["has_credential"] = bool(has_credential)
    return view


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
