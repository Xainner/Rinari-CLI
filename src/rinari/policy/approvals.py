"""Approval engine: consent management separate from policy and sandbox.

A policy decision of `ask` routes here. Grants have an explicit scope:

    once        valid for a single action (default)
    session     valid for the lifetime of this session
    project     valid for this project's sessions
    persistent  valid until revoked (stored outside the session)

Project grants never leak into global chat or another project
(harness.md section 79).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class GrantScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    PERSISTENT = "persistent"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    capability: str
    description: str
    target: str | None = None
    risk: str = "medium"
    session_id: str | None = None
    project_id: str | None = None
    rule_id: str = "default"
    reusable: bool = True
    choices: tuple[str, ...] = ("deny", "allow_once", "allow_session")
    cancellation: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    capability: str
    scope: GrantScope
    target: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    granted_at: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    granted: bool
    grant: ApprovalGrant | None = None
    reason: str = ""


AnswerPrompt = Callable[[ApprovalRequest], str]


class ApprovalEngine:
    """In-memory session/project grants plus an optional persistent store.

    `persistent_store` is a mapping key -> ApprovalGrant provided by the
    wiring (e.g. config_values). The engine treats it as read/write but
    persists nothing itself.
    """

    def __init__(
        self,
        prompt: AnswerPrompt | None = None,
        persistent_store: dict | None = None,
    ) -> None:
        self._prompt = prompt
        self._session_grants: list[ApprovalGrant] = []
        self._project_grants: list[ApprovalGrant] = []
        self._store = persistent_store
        self._audit: list[ApprovalOutcome] = []

    def audit(self) -> list[ApprovalOutcome]:
        return list(self._audit)

    def check(self, request: ApprovalRequest) -> ApprovalOutcome:
        grant = self._find_grant(request) if request.reusable else None
        if grant is not None:
            outcome = ApprovalOutcome(granted=True, grant=grant, reason="covered by existing grant")
            self._record(outcome)
            return outcome
        answer = (self._prompt(request) if self._prompt is not None else "n").strip().lower()
        if answer in ("y", "yes") and "allow_once" in request.choices:
            return self._record(self._issue(request, GrantScope.ONCE, reason="approved once"))
        if (
            answer in ("s", "session", "always-session")
            and request.reusable
            and "allow_session" in request.choices
        ):
            return self._record(
                self._issue(request, GrantScope.SESSION, reason="approved for session")
            )
        if answer in ("p", "project", "always-project"):
            return self._record(
                self._issue(request, GrantScope.PROJECT, reason="approved for project")
            )
        if answer in ("a", "always", "persistent"):
            return self._record(
                self._issue(request, GrantScope.PERSISTENT, reason="approved persistently")
            )
        outcome = ApprovalOutcome(granted=False, reason=f"denied (answer: {answer or 'no prompt'})")
        self._record(outcome)
        return outcome

    def _find_grant(self, request: ApprovalRequest) -> ApprovalGrant | None:
        for store, scope_filter in (
            (self._store.values() if self._store else [], GrantScope.PERSISTENT),
            (self._project_grants, GrantScope.PROJECT),
            (self._session_grants, GrantScope.SESSION),
        ):
            for grant in store:
                if grant.scope is not scope_filter:
                    continue
                if scope_filter is GrantScope.PROJECT and grant.project_id != request.project_id:
                    continue
                if scope_filter is GrantScope.SESSION and grant.session_id != request.session_id:
                    continue
                if self._matches(grant, request):
                    return grant
        return None

    @staticmethod
    def _matches(grant: ApprovalGrant, request: ApprovalRequest) -> bool:
        return grant.capability == request.capability and (
            grant.target is None or grant.target == request.target
        )

    def _issue(
        self, request: ApprovalRequest, scope: GrantScope, *, reason: str
    ) -> ApprovalOutcome:
        grant = ApprovalGrant(
            capability=request.capability,
            scope=scope,
            # A session grant intentionally covers subsequent targets of the
            # same capability in this session. Policy and sandbox boundaries
            # still apply before approval is consulted.
            target=None if scope is GrantScope.SESSION else request.target,
            session_id=request.session_id,
            project_id=request.project_id,
        )
        if scope is GrantScope.SESSION:
            self._session_grants.append(grant)
        elif scope is GrantScope.PROJECT:
            self._project_grants.append(grant)
        elif scope is GrantScope.PERSISTENT and self._store is not None:
            self._store[f"{request.session_id}:{request.target}"] = grant
        return ApprovalOutcome(granted=True, grant=grant, reason=reason)

    def _record(self, outcome: ApprovalOutcome) -> ApprovalOutcome:
        self._audit.append(outcome)
        return outcome

    def grants(self) -> list[ApprovalGrant]:
        return [
            *self._session_grants,
            *self._project_grants,
            *(self._store.values() if self._store else []),
        ]
