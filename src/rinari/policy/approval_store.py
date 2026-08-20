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
