"""Skill library methods for desktop clients (skill_library_v1).

Thin adapters over SkillService. Reading, toggling, editing and removing are
local and answer inline. Inspecting and installing may download a repository,
and the engine serves one request at a time: those run as jobs on their own
thread (`skill.job.start`) and report with `skill.job.completed` or
`skill.job.failed`, so a slow download never stalls a streaming turn.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.skills.manifest import SkillError

_JOB_ACTIONS = ("inspect", "install", "update")
_JOBS_KEPT = 50
_PROTOCOL_CODE = {
    "SKILL_NOT_FOUND": "NOT_FOUND",
    "ALREADY_EXISTS": "CONFLICT",
}


def skill_error(exc: SkillError) -> EngineProtocolError:
    """Protocol error with the skill's own code kept in the details."""
    return EngineProtocolError(
        _PROTOCOL_CODE.get(exc.code, INVALID_PARAMS),
        exc.message,
        details={"skill_code": exc.code, **exc.details},
    )


class SkillMethods:
    def __init__(self, services, emit: Callable[[dict], None]) -> None:
        self._services = services
        self._emit = emit
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def _skills(self):
        return self._services.skills

    # -- inline ------------------------------------------------------------------

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"skills": self._call(self._skills.library, _project(params))}

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _name(params)
        return {"skill": self._call(self._skills.detail, name, _project(params))}

    def read(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _name(params)
        path = params.get("path")
        if not isinstance(path, str) or not path:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'path' must be a non-empty string.")
        offset = _int(params, "offset", 0)
        limit = _int(params, "limit", 400)
        return self._call(
            self._skills.read_reference,
            name,
            path,
            _project(params),
            offset,
            limit,
            include_disabled=True,
        )

    def enable(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "skill": self._call(self._skills.set_enabled, _name(params), True, _project(params))
        }

    def disable(self, params: dict[str, Any]) -> dict[str, Any]:
        skill = self._call(self._skills.set_enabled, _name(params), False, _project(params))
        return {"skill": skill}

    def remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"removed": self._call(self._skills.remove, _name(params))}

    def write(self, params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("content")
        if not isinstance(content, str) or not content.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'content' must be a non-empty string.")
        return {"skill": self._call(self._skills.write, _name(params), content)}

    def import_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"candidates": self._call(self._skills.import_scan)}

    # -- jobs --------------------------------------------------------------------

    def job_start(self, params: dict[str, Any]) -> dict[str, Any]:
        action = params.get("action")
        if action not in _JOB_ACTIONS:
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'action' must be one of {', '.join(_JOB_ACTIONS)}."
            )
        work = self._work(action, params)
        job_id = self._services.ctx.ids.new("job")
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "action": action,
                "status": "running",
                "started_at": time.time(),
            }
            for stale in list(self._jobs)[:-_JOBS_KEPT]:
                self._jobs.pop(stale, None)

        def run() -> None:
            try:
                result = work()
                payload = {"job_id": job_id, "action": action, "result": result}
                self._finish(job_id, status="completed", result=result)
                self._emit(event("skill.job.completed", payload))
            except SkillError as exc:
                error = {"code": exc.code, "message": exc.message, "details": exc.details}
                self._finish(job_id, status="failed", error=error)
                self._emit(
                    event("skill.job.failed", {"job_id": job_id, "action": action, "error": error})
                )
            except Exception as exc:  # network, disk: report, never kill the engine
                error = {"code": "SKILL_JOB_FAILED", "message": str(exc), "details": {}}
                self._finish(job_id, status="failed", error=error)
                self._emit(
                    event("skill.job.failed", {"job_id": job_id, "action": action, "error": error})
                )

        threading.Thread(target=run, name=f"rinari-skill-{job_id}", daemon=True).start()
        return {"job_id": job_id, "action": action, "status": "running"}

    def job_get(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = params.get("job_id")
        with self._lock:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
            if job is None:
                raise EngineProtocolError("NOT_FOUND", f"Unknown skill job: {job_id}")
            return {"job": dict(job)}

    def _work(self, action: str, params: dict[str, Any]) -> Callable[[], dict[str, Any]]:
        expected = params.get("expected_hash")
        if expected is not None and not isinstance(expected, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'expected_hash' must be a string.")
        if action == "update":
            name = _name(params)
            force = params.get("force", False)
            if not isinstance(force, bool):
                raise EngineProtocolError(INVALID_PARAMS, "Param 'force' must be a boolean.")
            return lambda: self._installed(
                self._skills.update(name, expected_hash=expected, force=force)
            )
        source = params.get("source")
        if not isinstance(source, str) or not source.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'source' must be a non-empty string.")
        if action == "inspect":
            return lambda: self._skills.inspect(source)
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'name' must be a string.")
        return lambda: self._installed(
            self._skills.install(source, name or None, expected_hash=expected)
        )

    def _installed(self, manifest) -> dict[str, Any]:
        return {"skill": self._skills.detail(manifest.name)}

    def _finish(self, job_id: str, **fields) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(finished_at=time.time(), **fields)

    @staticmethod
    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SkillError as exc:
            raise skill_error(exc) from exc


def _name(params: dict[str, Any]) -> str:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'name' must be a non-empty string.")
    return name


def _int(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EngineProtocolError(INVALID_PARAMS, f"Param '{key}' must be a non-negative integer.")
    return value


def _project(params: dict[str, Any]) -> Path | None:
    root = params.get("project_root")
    if root is None:
        return None
    if not isinstance(root, str) or not Path(root).is_dir():
        raise EngineProtocolError(INVALID_PARAMS, "Param 'project_root' must be a folder.")
    return Path(root)


__all__ = ["SkillMethods", "skill_error"]
