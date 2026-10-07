"""Trabajos documentales: estado persistido, fases, progreso y cancelación.

Construir, editar, renderizar o calcular corre en un hilo del Engine que lanza
el trabajo pesado en un proceso hijo (`rinari.documents.worker`) con su propio
directorio temporal: una biblioteca nativa colgada se mata sin tumbar la
sesión. El estado vive en `document_jobs`; al arrancar, lo que quedó activo se
marca `interrupted` (no se reanuda solo).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from rinari.documents.contracts import (
    JOB_ACTIVE,
    JOB_CANCELLED,
    JOB_CANCELLING,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_TERMINAL,
    DocumentError,
    DocumentErrorCode,
)
from rinari.shared.clock import now_iso

MAX_WORKERS = 2
DEFAULT_WORKER_TIMEOUT_S = 300.0


class JobCancelled(Exception):
    pass


class JobHandle:
    """Lo que un trabajo puede hacer: fases, progreso medible y su proceso hijo."""

    def __init__(self, manager: JobManager, job_id: str, session_id: str) -> None:
        self._manager = manager
        self.id = job_id
        self.session_id = session_id
        self.cancel_event = threading.Event()
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled()

    def phase(self, name: str, *, done: int | None = None, total: int | None = None) -> None:
        self.check()
        self._manager._update(
            self.id, status=JOB_RUNNING, phase=name, progress_done=done, progress_total=total
        )

    def progress(self, done: int, total: int | None = None) -> None:
        self._manager._update(self.id, progress_done=done, progress_total=total)

    def run_worker(
        self, operation: str, request: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Corre `operation` en un proceso hijo y devuelve su resultado JSON."""
        self.check()
        with tempfile.TemporaryDirectory(prefix="rinari-doc-") as tmp:
            workdir = Path(tmp)
            (workdir / "request.json").write_text(
                json.dumps({"operation": operation, "request": request}, ensure_ascii=False),
                encoding="utf-8",
            )
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": str(workdir),
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                [sys.executable, "-m", "rinari.documents.worker", str(workdir)], **kwargs
            )
            with self._lock:
                self._process = process
            try:
                deadline = time.monotonic() + (timeout_s or DEFAULT_WORKER_TIMEOUT_S)
                while True:
                    try:
                        _, stderr = process.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if self.cancelled:
                            _kill(process)
                            raise JobCancelled() from None
                        if time.monotonic() > deadline:
                            _kill(process)
                            raise DocumentError(
                                DocumentErrorCode.RENDER_FAILED
                                if operation.startswith("render")
                                else DocumentErrorCode.DOCUMENT_LIMIT_EXCEEDED,
                                f"{operation} exceeded its time limit",
                            ) from None
            finally:
                with self._lock:
                    self._process = None
            result_path = workdir / "result.json"
            if not result_path.is_file():
                tail = (stderr or b"").decode("utf-8", "replace")[-800:]
                raise DocumentError(
                    DocumentErrorCode.RENDER_FAILED
                    if operation.startswith("render")
                    else DocumentErrorCode.VALIDATION_FAILED,
                    f"{operation} worker ended without a result",
                    details={"exit_code": process.returncode, "stderr": tail},
                )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if payload.get("error"):
                error = payload["error"]
                raise DocumentError(
                    DocumentErrorCode(error["code"]),
                    error["message"],
                    scope=error.get("scope"),
                    action=error.get("action"),
                    details=error.get("details"),
                )
            files = {}
            for key, rel in (payload.get("files") or {}).items():
                files[key] = (workdir / rel).read_bytes()
            return {**payload.get("result", {}), "_files": files}

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            _kill(process)


def _kill(process: subprocess.Popen) -> None:
    from rinari.tools.native.shell import _kill_tree

    _kill_tree(process)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)


class JobManager:
    """Uno por base de datos: los trabajos de la sesión, aunque cambie el cliente."""

    _instances: ClassVar[dict[str, JobManager]] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def for_context(cls, ctx) -> JobManager:
        key = str(getattr(ctx.db, "path", id(ctx.db)))
        with cls._instances_lock:
            manager = cls._instances.get(key)
            if manager is None or manager._db is not ctx.db:
                manager = cls(ctx)
                cls._instances[key] = manager
            return manager

    @classmethod
    def close_for(cls, ctx) -> None:
        """Cierre del Engine: se cancelan los trabajos vivos y se esperan sus hilos.

        Un hilo que siguiera escribiendo en una base ya cerrada tumba el proceso.
        """
        key = str(getattr(ctx.db, "path", id(ctx.db)))
        with cls._instances_lock:
            manager = cls._instances.pop(key, None)
        if manager is None:
            return
        with manager._lock:
            handles = list(manager._handles.values())
        for handle in handles:
            handle.cancel_event.set()
            handle.kill()
        manager._pool.shutdown(wait=True, cancel_futures=True)

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._db = ctx.db
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="rinari-doc")
        self._handles: dict[str, JobHandle] = {}
        self._done: dict[str, threading.Event] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        # Lo que quedó activo de un proceso anterior no sigue corriendo.
        self._db.execute(
            "UPDATE document_jobs SET status = ?, updated_at = ? WHERE status IN (?, ?, ?)",
            (JOB_INTERRUPTED, now_iso(ctx.clock), *JOB_ACTIVE),
        )

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def start(
        self,
        *,
        session_id: str,
        operation: str,
        request: dict[str, Any],
        runner: Callable[[JobHandle], dict[str, Any]],
    ) -> dict[str, Any]:
        job_id = self._ctx.ids.new("djob")
        now = now_iso(self._ctx.clock)
        self._db.execute(
            "INSERT INTO document_jobs (id, session_id, operation, status, request_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                session_id,
                operation,
                JOB_QUEUED,
                json.dumps(request, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        handle = JobHandle(self, job_id, session_id)
        done = threading.Event()
        with self._lock:
            self._handles[job_id] = handle
            self._done[job_id] = done
        self._emit(job_id)

        def run() -> None:
            try:
                self._update(job_id, status=JOB_RUNNING)
                result = runner(handle)
                status = result.pop("_status", JOB_SUCCEEDED)
                self._update(job_id, status=status, result=result, phase="publish")
            except JobCancelled:
                self._update(job_id, status=JOB_CANCELLED)
            except DocumentError as exc:
                status = JOB_CANCELLED if exc.code is DocumentErrorCode.CANCELLED else JOB_FAILED
                self._update(job_id, status=status, error=exc.to_dict())
            except Exception as exc:  # pragma: no cover - defensive
                self._update(
                    job_id,
                    status=JOB_FAILED,
                    error={
                        "code": DocumentErrorCode.VALIDATION_FAILED.value,
                        "message": f"{exc.__class__.__name__}: {exc}",
                        "retryable": False,
                    },
                )
            finally:
                with self._lock:
                    self._handles.pop(job_id, None)
                done.set()

        self._pool.submit(run)
        return self.get(job_id, session_id=session_id)

    def get(self, job_id: str, *, session_id: str) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT * FROM document_jobs WHERE id = ? AND session_id = ?", (job_id, session_id)
        )
        if row is None:
            raise DocumentError(DocumentErrorCode.NOT_FOUND, f"Unknown document job {job_id}")
        return _job_dict(row)

    def list(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM document_jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
        return [_job_dict(row) for row in rows]

    def cancel(self, job_id: str, *, session_id: str) -> dict[str, Any]:
        job = self.get(job_id, session_id=session_id)
        if job["status"] in JOB_TERMINAL:
            return job
        with self._lock:
            handle = self._handles.get(job_id)
        if handle is None:
            # Activo en la tabla pero sin hilo: era de un proceso anterior.
            self._update(job_id, status=JOB_INTERRUPTED)
            return self.get(job_id, session_id=session_id)
        self._update(job_id, status=JOB_CANCELLING)
        handle.cancel_event.set()
        handle.kill()
        return self.get(job_id, session_id=session_id)

    def wait(
        self, job_id: str, *, session_id: str, timeout_s: float, cancellation=None
    ) -> dict[str, Any]:
        with self._lock:
            done = self._done.get(job_id)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while done is not None and not done.is_set():
            if cancellation is not None and getattr(cancellation, "cancelled", False):
                self.cancel(job_id, session_id=session_id)
                done.wait(5)
                break
            if time.monotonic() >= deadline:
                break
            done.wait(0.1)
        return self.get(job_id, session_id=session_id)

    def _update(self, job_id: str, **fields: Any) -> None:
        columns = []
        values: list[Any] = []
        for key, value in fields.items():
            if key in ("result", "error"):
                columns.append(f"{key}_json = ?")
                values.append(json.dumps(value, ensure_ascii=False, default=str))
            elif value is not None or key in ("progress_done", "progress_total"):
                if key in ("progress_done", "progress_total") and value is None:
                    continue
                columns.append(f"{key} = ?")
                values.append(value)
        columns.append("updated_at = ?")
        values.append(now_iso(self._ctx.clock))
        # Un trabajo cancelado o interrumpido no vuelve a «running».
        guard = ""
        if fields.get("status") in (JOB_RUNNING,) or "status" not in fields:
            guard = (
                f" AND status NOT IN ('{JOB_CANCELLING}', '{JOB_CANCELLED}', '{JOB_INTERRUPTED}')"
            )
        self._db.execute(
            f"UPDATE document_jobs SET {', '.join(columns)} WHERE id = ?{guard}",
            (*values, job_id),
        )
        self._emit(job_id)

    def _emit(self, job_id: str) -> None:
        if not self._listeners:
            return
        row = self._db.query_one("SELECT * FROM document_jobs WHERE id = ?", (job_id,))
        if row is None:
            return
        payload = _job_dict(row)
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(payload)


def _job_dict(row: Any) -> dict[str, Any]:
    return {
        "job_id": row["id"],
        "session_id": row["session_id"],
        "operation": row["operation"],
        "status": row["status"],
        "phase": row["phase"],
        "progress": (
            {"done": row["progress_done"], "total": row["progress_total"]}
            if row["progress_done"] is not None
            else None
        ),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
