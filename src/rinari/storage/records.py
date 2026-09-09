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
    git_branch: str | None = None
    forked_from: str | None = None
    # Durable (name, version) pairs of skills active in this session
    # (phase 4: resume reconciliation; skill runtime arrives in phase 6).
    active_skills: tuple[tuple[str, str], ...] | None = None
    # Execution access selected for BUILD. PLAN/REVIEW clamp the effective
    # profile to read-only without discarding this preference.
    permission_profile: str = "workspace"
    # Session-scope Soul override. None inherits the global active Soul
    # (Soul 3.0 chain); never a free-form string, always a known soul id.
    soul_id: str | None = None


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
class TrustEntryRecord:
    """One trust grant for a canonical project path (phase 3).

    `fingerprint` is the project identity digest captured at grant time
    (see trust.store.fingerprint_for); a mismatch means revalidation.
    """

    canonical_path: str
    fingerprint: str | None = None
    trusted_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class ConfigValue:
    key: str
    value: str
    updated_at: str


@dataclass(slots=True)
class WorktreeBaselineRecord:
    """One pre-existing worktree entry captured at session start.

    `git_status` is the porcelain 2-char code (M, A, ?, R, ...);
    `blob_sha` is a sha256 of the file content (None for directories).
    """

    session_id: str
    path: str
    git_status: str
    blob_sha: str | None = None
    created_at: str = ""
