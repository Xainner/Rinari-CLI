"""GraphicControlService: the small coordinator computer use was missing.

Owns ONLY: the session backend, the grant store, observation vigencia and
the action dispatch ledger (not_dispatched / dispatched / unknown). Sessions,
models, artifacts, approvals and policy stay with the existing services; the
service receives the artifact store instead of importing desktop concepts.

A timeout after the bytes left the harness must never be retried blindly:
such actions are recorded as dispatch 'unknown' and require a fresh
observation before anything else touches the target.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from rinari.computer.backend import ComputerError, GraphicBackend
from rinari.computer.grants import GrantDenied, GrantStore, GraphicGrant


class GraphicControlService:
    def __init__(
        self,
        *,
        session_id: str,
        backend: GraphicBackend,
        grants: GrantStore | None = None,
        artifact_store: Any | None = None,
        clock=time.time,
    ) -> None:
        self.session_id = session_id
        self.backend = backend
        self.grants = grants or GrantStore()
        self.artifact_store = artifact_store
        self._clock = clock
        self._observations: dict[str, dict[str, Any]] = {}
        self._ledger: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- host side: grants are issued by explicit user gesture, never by tools --

    def issue_grant(
        self, target: str, scopes: list | tuple | set | frozenset, ttl_s: float, *, note: str = ""
    ) -> GraphicGrant:
        return self.grants.issue(self.session_id, target, scopes, ttl_s, note=note)

    def revoke_grant(self, grant_id: str) -> bool:
        return self.grants.revoke(grant_id)

    # -- status (grantless: tells the user nothing is authorized) --

    def state(self) -> dict[str, Any]:
        with self._lock:
            observations = len(self._observations)
            actions = len(self._ledger)
        try:
            targets = self.backend.targets()
        except ComputerError as exc:
            return {
                "backend": self.backend.name,
                "available": False,
                "error": f"{exc.code}: {exc.message}",
                "observations": observations,
                "actions": actions,
            }
        return {
            "backend": self.backend.name,
            "available": True,
            "targets": targets,
            "observations": observations,
            "actions": actions,
        }

    # -- observations --

    def note_observation(self, target: str) -> dict[str, Any]:
        record = {
            "observation_id": uuid.uuid4().hex[:12],
            "target_id": target,
            "backend": self.backend.name,
        }
        with self._lock:
            self._observations[target] = record
        return record

    def check_observation(self, target: str, observation_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._observations.get(target)
        if record is None or record.get("observation_id") != observation_id:
            raise ComputerError(
                "INVALID_ARGUMENT",
                "Stale or unknown observation_id for this target; "
                "capture a fresh observation and retry",
            )
        return record

    def _record_action(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ledger.append(entry)
        return entry

    # -- capture --

    def capture(
        self,
        target: str,
        *,
        send_to_model: bool,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.grants.check(self.session_id, target, "observe")
        if send_to_model:
            self.grants.check(self.session_id, target, "send")
        if self.artifact_store is None:
            raise ComputerError("BACKEND_UNAVAILABLE", "session media store is unavailable")
        frame = self.backend.capture(target, cancelled=cancelled)
        record = self.artifact_store.create(
            self.session_id,
            "computer",
            f"capture-{int(self._clock())}-{uuid.uuid4().hex[:8]}.png",
            frame.png,
            content_type="image/png",
            provenance=f"computer.capture:{target}",
            summary=f"Desktop capture {target}",
        )
        observation = self.note_observation(target)
        return {
            "uri": record.uri(),
            "artifact": record.uri(),
            "sha256": record.sha256,
            "bytes": len(frame.png),
            "width": frame.width,
            "height": frame.height,
            "dpi_scale": frame.dpi_scale,
            "mime_type": record.content_type,
            "target_id": target,
            "observation_id": observation["observation_id"],
            "captured_at": int(self._clock()),
            "record": record,
            "dispatch": "dispatched",
        }

    # -- input --

    def click(
        self,
        target: str,
        x: float,
        y: float,
        *,
        observation_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.grants.check(self.session_id, target, "input")
        if observation_id is not None:
            self.check_observation(target, observation_id)
        action_id = uuid.uuid4().hex[:12]
        try:
            result = self.backend.click(target, x, y, cancelled=cancelled)
        except ComputerError as exc:
            self._record_action(
                {
                    "action_id": action_id,
                    "kind": "click",
                    "target": target,
                    "dispatch": "unknown" if exc.code == "CANCELLED" else "not_dispatched",
                    "observation_id": observation_id,
                    "error": f"{exc.code}: {exc.message}",
                }
            )
            raise
        return self._record_action(
            {
                "action_id": action_id,
                "kind": "click",
                "target": target,
                "dispatch": "dispatched",
                "observation_id": observation_id,
                "result": result,
            }
        )

    def type_text(
        self,
        target: str,
        text: str,
        *,
        submit: bool = False,
        observation_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.grants.check(self.session_id, target, "input")
        if observation_id is not None:
            self.check_observation(target, observation_id)
        action_id = uuid.uuid4().hex[:12]
        try:
            result = self.backend.type_text(target, text, submit=submit, cancelled=cancelled)
        except ComputerError as exc:
            self._record_action(
                {
                    "action_id": action_id,
                    "kind": "type",
                    "target": target,
                    "dispatch": "unknown" if exc.code == "CANCELLED" else "not_dispatched",
                    "observation_id": observation_id,
                    "error": f"{exc.code}: {exc.message}",
                }
            )
            raise
        return self._record_action(
            {
                "action_id": action_id,
                "kind": "type",
                "target": target,
                "dispatch": "dispatched",
                "observation_id": observation_id,
                "result": result,
            }
        )


__all__ = ["GrantDenied", "GraphicControlService"]
