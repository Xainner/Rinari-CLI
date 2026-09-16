"""Graphic-control grants: explicit, scoped, expiring user leases.

A grant is issued ONLY by the session host on a deliberate user gesture
(CLI/desktop approval surface) — never by the model, never by a tool.
Computer tools fail closed without a valid grant covering the target,
session and scope. Scopes are separate by design: observe (capture state),
input (click/type) and send (transmit captures to the model).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

SCOPES = ("observe", "input", "send")


class GrantDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GraphicGrant:
    grant_id: str
    session_id: str
    target: str
    scopes: frozenset
    expires_at: float
    note: str = ""


class GrantStore:
    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._grants: dict[str, GraphicGrant] = {}
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def issue(
        self,
        session_id: str,
        target: str,
        scopes: list | tuple | set | frozenset,
        ttl_s: float,
        *,
        note: str = "",
    ) -> GraphicGrant:
        unknown = set(scopes) - set(SCOPES)
        if unknown:
            raise GrantDenied(f"unknown grant scopes: {sorted(unknown)}")
        if not scopes:
            raise GrantDenied("grant must include at least one scope")
        if ttl_s <= 0:
            raise GrantDenied("grant ttl must be positive")
        grant = GraphicGrant(
            grant_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            target=target,
            scopes=frozenset(scopes),
            expires_at=self._clock() + ttl_s,
            note=note,
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            if grant_id not in self._grants:
                return False
            self._revoked.add(grant_id)
            return True

    def check(self, session_id: str, target: str, scope: str) -> GraphicGrant:
        with self._lock:
            live = [g for g in self._grants.values() if g.grant_id not in self._revoked]
        now = self._clock()
        if scope not in SCOPES:
            raise GrantDenied(f"unknown scope: {scope}")
        matching = [g for g in live if g.session_id == session_id and g.target == target]
        if not matching:
            raise GrantDenied(
                "no graphic-control grant for this session and target; "
                "the user must issue one before computer tools can act"
            )
        valid = [g for g in matching if g.expires_at > now]
        if not valid:
            raise GrantDenied("graphic-control grant expired; ask the user to re-issue it")
        scoped = [g for g in valid if scope in g.scopes]
        if not scoped:
            raise GrantDenied(
                f"grant does not cover scope {scope!r}; the user must issue a grant including it"
            )
        return max(scoped, key=lambda g: g.expires_at)


__all__ = ["SCOPES", "GrantDenied", "GrantStore", "GraphicGrant"]
