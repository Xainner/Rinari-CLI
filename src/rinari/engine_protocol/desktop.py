"""Desktop adapters for engine-owned workspace transitions and bounded previews."""

from pathlib import Path

from rinari.engine_protocol.errors import INVALID_PARAMS, TURN_RUNNING, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.engine_protocol.snapshots import session_to_dict


class DesktopWorkspace:
    def __init__(self, server):
        self.server = server
        self.services = server._services

    def move(self, params):
        with self.server._turns._lock:
            return self._move(params)

    def _move(self, params):
        ref = params.get("session_id")
        if not isinstance(ref, str) or not ref:
            raise EngineProtocolError(INVALID_PARAMS, "session_id is required.")
        record = self.services.sessions.show(ref)
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

    def resolve_file(self, params):
        if not isinstance(params.get("session_id"), str) or not params["session_id"]:
            raise EngineProtocolError(INVALID_PARAMS, "session_id is required.")
        record = self.services.sessions.show(params.get("session_id"))
        supplied = params.get("path")
        if not isinstance(supplied, str) or not supplied or len(supplied) > 32768:
            raise EngineProtocolError(INVALID_PARAMS, "A file path is required.")
        root = Path(record.current_cwd)
        events = self.services.ctx.event_repo.list(record.id)
        turn_id = params.get("turn_id")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise EngineProtocolError(INVALID_PARAMS, "Invalid turn_id.")
        if turn_id:
            # Old turns have no explicit root. Replay workspace transitions up
            # to the turn's first event instead of using today's workspace.
            root = Path(record.created_cwd)
            found = False
            for row in events:
                if row.type == "SessionPromotedToProject" and row.payload.get("project_root"):
                    root = Path(row.payload["project_root"])
                if row.type == "session.moved":
                    root = Path(row.payload["cwd"])
                if row.turn_id == turn_id or row.payload.get("turn_id") == turn_id:
                    root = Path(row.payload.get("workspace_root") or root)
                    found = True
                    break
            if not found:
                raise EngineProtocolError(INVALID_PARAMS, "Unknown turn provenance.")
        root = root.resolve()
        path = Path(supplied)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            raise EngineProtocolError("PERMISSION_DENIED", "File is outside this turn's workspace.")
        if path.is_relative_to(self.services.changes.blobs.root.resolve()):
            raise EngineProtocolError("PERMISSION_DENIED", "Engine-private file.")
        if not path.is_file():
            raise EngineProtocolError("NOT_FOUND", "File no longer exists.")
        return path, root

    def read(self, params):
        path, _ = self.resolve_file(params)
        limit = 512 * 1024
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise EngineProtocolError("FILE_TOO_LARGE", "Preview is limited to 512 KiB.")
        try:
            if b"\0" in data:
                raise UnicodeError()
            content = data.decode("utf-8-sig")
        except UnicodeError:
            raise EngineProtocolError(
                "UNSUPPORTED_FILE", "Preview supports UTF-8 text files."
            ) from None
        return {
            "path": str(path),
            "name": path.name,
            "content": content,
            "language": path.suffix.lstrip(".").lower(),
            "size": len(data),
        }
