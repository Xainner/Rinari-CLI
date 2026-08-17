"""Storage-level value objects (rows as typed records).

These are persistence shapes, not domain services. Domain packages
import them; repositories map rows to/from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProviderRecord:
    id: str
    alias: str
    type: str
    auth_method: str
    account_hint: str | None
    endpoint: str | None
    settings: dict[str, Any]
    default_model_id: str | None
    last_used_model_id: str | None
    status_connected: bool | None
    status_checked_at: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ProviderCredentialRef:
    provider_id: str
    secret_ref: str
    method: str
    updated_at: str


@dataclass(slots=True)
class ModelRecord:
    id: str
    alias: str
    provider_id: str
    provider_model_id: str
    settings: dict[str, Any]
    capabilities: dict[str, Any] | None
    availability: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ProjectRecord:
    id: str
    canonical_root: str
    git_fingerprint: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SessionRecord:
    id: str
    kind: str
    title: str | None
    project_id: str | None
    project_root_snapshot: str | None
    created_cwd: str
    current_cwd: str
    provider_id: str
    model_id: str
    profile_id: str
    mode: str
    state: str
    compact_state: dict[str, Any] | None
    created_at: str
    updated_at: str
    last_active_at: str


@dataclass(slots=True)
class SessionEventRecord:
    id: str
    session_id: str
    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(slots=True)
class SessionMessageRecord:
    """One persisted conversation message (provider-agnostic ChatMessage).

    `tool_calls` uses the wire shape: list of {id, name, arguments}.
    """

    id: str
    session_id: str
    seq: int
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    created_at: str = ""


@dataclass(slots=True)
class ConfigValue:
    key: str
    value: str
    updated_at: str
