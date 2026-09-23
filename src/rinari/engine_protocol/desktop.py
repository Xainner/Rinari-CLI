"""Desktop adapters for workspace transitions and provenance-bound file previews."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, TURN_RUNNING, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.engine_protocol.snapshots import session_to_dict
from rinari.policy.engine import is_sensitive_file

PREVIEW_LIMIT = 512 * 1024
MAX_FILE_WATCHES = 64
WATCH_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class ResolvedFile:
    path: Path
    observed_path: Path
    workspace: Path
    provenance: str
    after_hash: str | None = None
    sensitive: bool = False


@dataclass(slots=True)
class _Watch:
    watch_id: str
    session_id: str
    path: Path
    canonical_path: Path
    turn_id: str | None
    fingerprint: tuple[Any, ...] | None
    revision: int = 0


def _same_path(left: Path, right: Path) -> bool:
    """Compare stored provenance with the currently resolved target.

    ``left`` was canonical when persisted and must not be resolved again: an
    attacker can replace that lexical path with a symlink after the turn. If
    both sides followed it now, the replacement target would inherit the old
    authorization.
    """

    stored = os.path.normcase(os.path.abspath(os.path.normpath(str(left))))
    current = os.path.normcase(str(right.resolve()))
    return stored == current


def _read_bounded(path: Path, canonical_path: Path) -> bytes:
    """Revalidate the location and opened regular file before returning any bytes."""
    if not _same_path(canonical_path, path):
        raise EngineProtocolError("PERMISSION_DENIED", "File target changed.")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise EngineProtocolError("UNSUPPORTED_FILE", "Preview requires a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    # Open the already canonical location, never a newly supplied link target.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(canonical_path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise EngineProtocolError("PERMISSION_DENIED", "File changed while opening.")
        data = stream.read(PREVIEW_LIMIT + 1)
        if not _same_path(canonical_path, path) or not os.path.samestat(opened, path.stat()):
            raise EngineProtocolError("PERMISSION_DENIED", "File target changed.")
    return data


def _fingerprint(path: Path, canonical_path: Path) -> tuple[Any, ...] | None:
    """A bounded fingerprint suitable for polling preview-sized files."""

    try:
        if not _same_path(canonical_path, path):
            return ("target_changed",)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return ("other", info.st_mtime_ns, info.st_size, info.st_ino)
        digest = None
        if info.st_size <= PREVIEW_LIMIT:
            digest = hashlib.sha256(_read_bounded(path, canonical_path)).hexdigest()
        return ("file", info.st_mtime_ns, info.st_size, info.st_ino, digest)
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, EngineProtocolError):
        return ("unavailable",)


class FileWatchRegistry:
    """Bounded polling registry; events carry invalidation, never file content."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        max_watches: int = MAX_FILE_WATCHES,
        interval: float = WATCH_INTERVAL_SECONDS,
        auto_start: bool = True,
    ) -> None:
        self._emit = emit
        self._max_watches = max_watches
        self._interval = interval
        self._items: dict[str, _Watch] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        if auto_start:
            self._worker = threading.Thread(
                target=self._run,
                name="rinari-file-watches",
                daemon=True,
            )
            self._worker.start()

    def add(
        self,
        session_id: str,
        path: Path,
        turn_id: str | None,
        *,
        canonical_path: Path | None = None,
    ) -> str:
        canonical_path = canonical_path or path.resolve()
        with self._lock:
            if self._closed.is_set():
                raise EngineProtocolError("FILE_WATCH_CLOSED", "File watches are closed.")
            if len(self._items) >= self._max_watches:
                raise EngineProtocolError(
                    "FILE_WATCH_LIMIT", "Close a file preview before watching another."
                )
            watch_id = f"fw_{secrets.token_hex(16)}"
            self._items[watch_id] = _Watch(
                watch_id=watch_id,
                session_id=session_id,
                path=path,
                canonical_path=canonical_path,
                turn_id=turn_id,
                fingerprint=_fingerprint(path, canonical_path),
            )
            return watch_id

    def remove(self, watch_id: str) -> bool:
        with self._lock:
            return self._items.pop(watch_id, None) is not None

    def close_session(self, session_id: str) -> None:
        with self._lock:
            stale = [key for key, item in self._items.items() if item.session_id == session_id]
            for key in stale:
                self._items.pop(key, None)

    def scan_once(self) -> None:
        """One deterministic scan; public as a test seam."""

        with self._lock:
            snapshot = list(self._items.values())
        for item in snapshot:
            current = _fingerprint(item.path, item.canonical_path)
            with self._lock:
                live = self._items.get(item.watch_id)
                if live is None or live.fingerprint == current:
                    continue
                previous = live.fingerprint
                live.fingerprint = current
                live.revision += 1
                revision = live.revision
            state = "deleted" if current is None else "recreated" if previous is None else "changed"
            self._emit(
                event(
                    "workspace.file.changed",
                    {
                        "watch_id": item.watch_id,
                        "session_id": item.session_id,
                        "path": str(item.path),
                        "revision": revision,
                        "state": state,
                    },
                )
            )

    def close(self) -> None:
        self._closed.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(1.0, self._interval * 3))
        with self._lock:
            self._items.clear()

    def _run(self) -> None:
        while not self._closed.wait(self._interval):
            self.scan_once()


class DesktopWorkspace:
    def __init__(self, server):
        self.server = server
        self.services = server._services
        self._watches = FileWatchRegistry(server._turns.emit_external)

    def close(self) -> None:
        self._watches.close()

    def close_session(self, session_id: str) -> None:
        self._watches.close_session(session_id)

    def move(self, params):
        with self.server._turns._lock:
            return self._move(params)

    def _move(self, params):
        ref = params.get("session_id")
        if not isinstance(ref, str) or not ref:
            raise EngineProtocolError(INVALID_PARAMS, "session_id is required.")
        record = self.services.sessions.show(ref)
        if self.server._attachment_jobs.has_pending(record.id):
            raise EngineProtocolError(
                "SESSION_BUSY", "Finish or cancel attachment preparation before moving."
            )
        if self.server._previews.busy(record.id):
            raise EngineProtocolError(
                "SESSION_BUSY", "Close the development preview before moving."
            )
        with self.server._turns._lock:
            prior_turns = [
                t for t in self.server._turns._turns.values() if t.session_id == record.id
            ]
        for turn in prior_turns:
            ctx = turn.session.context.tool_ctx if turn.session else None
            if ctx and (
                (ctx.processes and any(p.process.poll() is None for p in ctx.processes.list()))
                or (ctx.pty and any(not p.exited for p in ctx.pty.list()))
            ):
                raise EngineProtocolError(
                    "SESSION_BUSY", "Terminate session processes before moving."
                )
        if self.server._turns.has_active_turn(record.id):
            raise EngineProtocolError(TURN_RUNNING, "Wait for the turn to finish before moving.")
        if any(p["alive"] and p["session_id"] == record.id for p in self.server._pty.list()):
            raise EngineProtocolError("SESSION_BUSY", "Terminate session terminals before moving.")
        if self.server._turns.queue_list(record.id).get("pending"):
            raise EngineProtocolError("SESSION_BUSY", "Clear the prompt queue before moving.")
        project_id = params.get("project_id")
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise EngineProtocolError(INVALID_PARAMS, "Invalid destination project.")
        result = self.services.sessions.move(record.id, project_id)
        self.server._turns.emit_external(
            event(
                "session.moved",
                {
                    "session_id": record.id,
                    "project_id": result.project_id,
                    "cwd": result.current_cwd,
                },
            )
        )
        return {"session": session_to_dict(result)}

    def resolve_file(self, params) -> ResolvedFile:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(INVALID_PARAMS, "session_id is required.")
        record = self.services.sessions.show(session_id)
        supplied = params.get("path")
        if not isinstance(supplied, str) or not supplied or len(supplied) > 32768:
            raise EngineProtocolError(INVALID_PARAMS, "A file path is required.")

        turn_id = params.get("turn_id")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise EngineProtocolError(INVALID_PARAMS, "Invalid turn_id.")

        root = Path(record.current_cwd)
        turn_found = turn_id is None
        changeset = None
        if turn_id:
            # Replay historical workspace transitions for paths relative to an
            # old turn. Exact external provenance can still stand on its own.
            root = Path(record.created_cwd)
            for row in self.services.ctx.event_repo.list(record.id):
                if row.type == "SessionPromotedToProject" and row.payload.get("project_root"):
                    root = Path(row.payload["project_root"])
                if row.type == "session.moved":
                    root = Path(row.payload["cwd"])
                if row.turn_id == turn_id or row.payload.get("turn_id") == turn_id:
                    root = Path(row.payload.get("workspace_root") or root)
                    turn_found = True
                    break
            changeset = self.services.ctx.turn_change_repo.get_by_turn(turn_id)
            if changeset is not None and changeset["session_id"] != record.id:
                raise EngineProtocolError(
                    "PERMISSION_DENIED", "Turn provenance belongs to another session."
                )

        root = root.resolve()
        supplied_path = Path(supplied)
        candidate = supplied_path if supplied_path.is_absolute() else root / supplied_path
        observed_path = Path(os.path.abspath(os.path.normpath(str(candidate))))
        path = candidate.resolve()

        if path.is_relative_to(self.services.changes.blobs.root.resolve()):
            raise EngineProtocolError("PERMISSION_DENIED", "Engine-private file.")

        if turn_found and path.is_relative_to(root):
            resolved = ResolvedFile(
                path=path,
                observed_path=observed_path,
                workspace=root,
                provenance="workspace",
                sensitive=is_sensitive_file(path),
            )
        else:
            private_root = self.services.ctx.layout.root.resolve()
            if path.is_relative_to(private_root):
                raise EngineProtocolError("PERMISSION_DENIED", "Engine-private file.")
            changed = self._external_change(changeset, path)
            if changed is None:
                message = (
                    "Unknown turn provenance."
                    if turn_id and not turn_found and changeset is None
                    else "File is outside this turn's workspace."
                )
                code = INVALID_PARAMS if message.startswith("Unknown") else "PERMISSION_DENIED"
                raise EngineProtocolError(code, message)
            resolved = ResolvedFile(
                path=path,
                observed_path=observed_path,
                workspace=root,
                provenance="turn_changeset",
                after_hash=changed.get("after_hash"),
                sensitive=bool(changed.get("sensitive")),
            )

        if not path.is_file():
            raise EngineProtocolError("NOT_FOUND", "File no longer exists.")
        return resolved

    @staticmethod
    def _external_change(changeset: dict[str, Any] | None, path: Path) -> dict[str, Any] | None:
        if changeset is None:
            return None
        for row in changeset.get("files", []):
            if row.get("kind") not in {"created", "modified", "renamed"}:
                continue
            if not row.get("after_exists") or not isinstance(row.get("absolute_path"), str):
                continue
            if _same_path(Path(row["absolute_path"]), path):
                return row
        return None

    def read(self, params):
        return self._preview(self.resolve_file(params))

    def watch(self, params):
        resolved = self.resolve_file(params)
        # Observe the lexical location the user opened. Watching only the
        # resolved target would miss replacing that path with a symlink.
        watch_id = self._watches.add(
            params["session_id"],
            resolved.observed_path,
            params.get("turn_id"),
            canonical_path=resolved.path,
        )
        try:
            preview = self._preview(resolved)
        except Exception:
            self._watches.remove(watch_id)
            raise
        return {"watch_id": watch_id, "preview": preview}

    def unwatch(self, params):
        watch_id = params.get("watch_id")
        if not isinstance(watch_id, str) or not watch_id or len(watch_id) > 128:
            raise EngineProtocolError(INVALID_PARAMS, "watch_id is required.")
        self._watches.remove(watch_id)
        return {}

    @staticmethod
    def _preview(resolved: ResolvedFile) -> dict[str, Any]:
        data = _read_bounded(resolved.observed_path, resolved.path)
        if len(data) > PREVIEW_LIMIT:
            raise EngineProtocolError("FILE_TOO_LARGE", "Preview is limited to 512 KiB.")
        try:
            if b"\0" in data:
                raise UnicodeError()
            content = data.decode("utf-8-sig")
        except UnicodeError:
            raise EngineProtocolError(
                "UNSUPPORTED_FILE", "Preview supports UTF-8 text files."
            ) from None
        current_hash = hashlib.sha256(data).hexdigest()
        return {
            "path": str(resolved.path),
            "name": resolved.path.name,
            "content": content,
            "language": resolved.path.suffix.lstrip(".").lower(),
            "size": len(data),
            "provenance": resolved.provenance,
            "changed_since_turn": bool(
                resolved.provenance == "turn_changeset"
                and resolved.after_hash
                and current_hash != resolved.after_hash
            ),
            "sensitive": resolved.sensitive,
        }
