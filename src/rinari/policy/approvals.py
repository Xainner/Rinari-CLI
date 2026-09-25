"""Approval engine: consent management separate from policy and sandbox.

A policy decision of `ask` routes here. Grants have an explicit scope:

    once        valid for a single action (default)
    session     valid for the lifetime of this session
    project     "always in this project" (or in every loose chat): persisted
                in policies/project_grants.json until revoked in Settings
    persistent  valid until revoked (stored outside the session)

Grants are bound to the policy rule that asked (`rule_id`): allowing a push
does not also allow writing outside the project. Hard-list asks
(`reusable=False`) never consult or create a lasting grant.

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
    # `exact`: grants bind to this target (never widened to the capability)
    # and a capability-wide grant cannot cover this request.
    binding_mode: str = "capability"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    capability: str
    scope: GrantScope
    target: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    granted_at: str = ""
    # Rule that asked; None = legacy capability-wide grant (task grants).
    rule_id: str | None = None
    grant_id: str | None = None


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
        session_grants: list[ApprovalGrant] | None = None,
        project_store=None,
    ) -> None:
        self._prompt = prompt
        # The host may inject a session-bound list so grants outlive the tool
        # runtime of a single turn (desktop turns rebuild it every time) and
        # die with the session, a revocation or a new engine process.
        self._session_grants: list[ApprovalGrant] = (
            session_grants if session_grants is not None else []
        )
        self._project_grants: list[ApprovalGrant] = []
        # ProjectGrantStore (approval_store): "always" grants survive the
        # session and the process. Without it they live in memory only.
        self._project_store = project_store
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
        # Legacy (capability-bound) requests keep the CLI answers p/a. An
        # exact-bound request (peer messaging) is honored only for the choices
        # it offered: hiding a button in a client is not a security boundary.
        wide_ok = request.binding_mode != "exact"
        if answer in ("p", "project", "always-project") and (
            "allow_project" in request.choices
            or (wide_ok and request.reusable and self._project_store is None)
        ):
            return self._record(
                self._issue(request, GrantScope.PROJECT, reason="approved for project")
            )
        if answer in ("a", "always", "persistent") and (
            wide_ok or "allow_persistent" in request.choices
        ):
            return self._record(
                self._issue(request, GrantScope.PERSISTENT, reason="approved persistently")
            )
        outcome = ApprovalOutcome(granted=False, reason=f"denied (answer: {answer or 'no prompt'})")
        self._record(outcome)
        return outcome

    def _find_grant(self, request: ApprovalRequest) -> ApprovalGrant | None:
        project_grants = list(self._project_grants)
        if self._project_store is not None and request.project_id:
            project_grants.extend(self._project_store.grants_for(request.project_id))
        for store, scope_filter in (
            (self._store.values() if self._store else [], GrantScope.PERSISTENT),
            (project_grants, GrantScope.PROJECT),
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
        if grant.capability != request.capability:
            return False
        if grant.rule_id is not None and grant.rule_id != request.rule_id:
            return False
        if request.binding_mode == "exact":
            # A wildcard grant never covers an exact request; the target must
            # match byte for byte.
            return grant.target is not None and grant.target == request.target
        if grant.target is None or grant.target == request.target:
            return True
        # A folder granted for writes covers what is inside it.
        return (
            request.capability == "fs.write"
            and request.target is not None
            and _inside(request.target, grant.target)
        )

    def _issue(
        self, request: ApprovalRequest, scope: GrantScope, *, reason: str
    ) -> ApprovalOutcome:
        if scope is GrantScope.PROJECT and self._project_store is not None:
            grant = self._project_store.add(
                scope_key=request.project_id or "chats",
                capability=request.capability,
                rule_id=request.rule_id,
                target=_lasting_target(request),
                description=request.description,
            )
            return ApprovalOutcome(granted=True, grant=grant, reason="approved always here")
        grant = ApprovalGrant(
            capability=request.capability,
            scope=scope,
            rule_id=request.rule_id,
            # A session grant intentionally covers subsequent targets of the
            # same capability in this session (legacy binding). Exact-bound
            # requests keep their target on every scope. Policy and sandbox
            # boundaries still apply before approval is consulted.
            target=(
                request.target
                if request.binding_mode == "exact" or scope is not GrantScope.SESSION
                else None
            ),
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


def _inside(child: str, parent: str) -> bool:
    from pathlib import Path

    try:
        child_path, parent_path = Path(child).resolve(), Path(parent).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return child_path == parent_path or parent_path in child_path.parents


def _lasting_target(request: ApprovalRequest) -> str | None:
    """What an "always" grant keeps: the host or destination for exact rules,
    the folder for writes outside the project, nothing (the rule) otherwise."""
    if request.binding_mode == "exact":
        return request.target
    if request.capability == "fs.write" and request.target:
        from pathlib import Path

        return str(Path(request.target).resolve().parent)
    return None
