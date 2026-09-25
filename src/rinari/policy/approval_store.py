"""Persistent approval grants store (commands.md 38).

Grants with `persistent` scope must survive the session (approvals.py module
docstring). The store is a JSON file under $RINARI_HOME/policies/; it maps
grant keys to grant dicts. Secrets are never stored here - only capability,
scope, target, ids and timestamps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rinari.policy.approvals import ApprovalGrant, GrantScope

GRANTS_FILENAME = "approval_grants.json"
PROJECT_GRANTS_FILENAME = "project_grants.json"


class ApprovalStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, ApprovalGrant]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        out: dict[str, ApprovalGrant] = {}
        for key, value in (raw or {}).items():
            grant = _grant_from_dict(value)
            if grant is not None:
                out[key] = grant
        return out

    def save(self, grants: dict[str, ApprovalGrant]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "capability": grant.capability,
                "scope": grant.scope.value,
                "target": grant.target,
                "session_id": grant.session_id,
                "project_id": grant.project_id,
                "granted_at": grant.granted_at,
            }
            for key, grant in grants.items()
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def key(self, session_id: str | None, target: str | None) -> str:
        return f"{session_id or '-'}:{target or '-'}"

    @property
    def path(self) -> Path:
        return self._path


def _grant_from_dict(value: dict[str, Any]) -> ApprovalGrant | None:
    try:
        return ApprovalGrant(
            capability=value["capability"],
            scope=GrantScope(value["scope"]),
            target=value.get("target"),
            session_id=value.get("session_id"),
            project_id=value.get("project_id"),
            granted_at=value.get("granted_at", ""),
        )
    except (KeyError, ValueError):
        return None


def store_path_for(layout) -> Path:
    return layout.dir("policies") / GRANTS_FILENAME


class ProjectGrantStore:
    """ "Always in this project" grants (or in every loose chat: scope "chats").

    One JSON file under $RINARI_HOME/policies, re-read on every lookup so a
    revocation from Settings applies to the running turn. Never holds secrets:
    capability, rule, target (a host, a folder, a destination), timestamps.
    """

    def __init__(self, path: Path, *, now=None, new_id=None) -> None:
        import threading
        import time
        import uuid

        self._path = path
        self._lock = threading.Lock()
        self._now = now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._new_id = new_id or (lambda: "grant_" + uuid.uuid4().hex[:16])

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("grants") if isinstance(raw, dict) else None
        return [row for row in rows or [] if isinstance(row, dict) and row.get("id")]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "grants": rows}, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def list(self, scope_key: str | None = None) -> list[dict[str, Any]]:
        rows = self._read()
        return [row for row in rows if scope_key is None or row.get("scope") == scope_key]

    def grants_for(self, scope_key: str) -> list[ApprovalGrant]:
        return [
            ApprovalGrant(
                capability=row["capability"],
                scope=GrantScope.PROJECT,
                target=row.get("target"),
                project_id=row.get("scope"),
                granted_at=row.get("granted_at", ""),
                rule_id=row.get("rule_id"),
                grant_id=row["id"],
            )
            for row in self.list(scope_key)
            if row.get("capability")
        ]

    def add(
        self,
        *,
        scope_key: str,
        capability: str,
        rule_id: str | None,
        target: str | None,
        description: str = "",
    ) -> ApprovalGrant:
        with self._lock:
            rows = self._read()
            existing = next(
                (
                    row
                    for row in rows
                    if row.get("scope") == scope_key
                    and row.get("capability") == capability
                    and row.get("rule_id") == rule_id
                    and row.get("target") == target
                ),
                None,
            )
            if existing is None:
                existing = {
                    "id": self._new_id(),
                    "scope": scope_key,
                    "capability": capability,
                    "rule_id": rule_id,
                    "target": target,
                    "description": description[:300],
                    "granted_at": self._now(),
                }
                rows.append(existing)
                self._write(rows)
        return ApprovalGrant(
            capability=capability,
            scope=GrantScope.PROJECT,
            target=target,
            project_id=scope_key,
            granted_at=existing["granted_at"],
            rule_id=rule_id,
            grant_id=existing["id"],
        )

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [row for row in rows if row.get("id") != grant_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True


def project_store_path_for(layout) -> Path:
    return layout.dir("policies") / PROJECT_GRANTS_FILENAME
