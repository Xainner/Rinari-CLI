"""Host-only media operations. Model access remains behind runtime tools."""

import hashlib
from pathlib import Path

from rinari.artifacts.transfer import import_file
from rinari.cli.agent_runtime import _caller_for
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.models.images import ImageReference


def register_media(dispatcher, services):
    def configure_vision(params):
        if type(params.get("enabled")) is not bool:
            raise EngineProtocolError(INVALID_PARAMS, "Vision must be boolean")
        record = services.models.set_vision(params["ref"], params["enabled"])
        return {"vision": record.capabilities["vision"]}

    def listing(params):
        from rinari.artifacts.limits import limit
        from rinari.artifacts.store import _record_from_row

        session_id = params["session_id"]
        offset = max(0, int(params.get("offset", 0)))
        rows = services.ctx.db.query(
            "SELECT * FROM artifacts WHERE namespace='media' AND session_ref=? "
            "ORDER BY created_at DESC,id LIMIT 100 OFFSET ?",
            (session_id, offset),
        )
        total = services.ctx.db.query(
            "SELECT COUNT(*) AS count,COALESCE(SUM(byte_count),0) AS bytes FROM artifacts "
            "WHERE namespace='media' AND session_ref=?",
            (session_id,),
        )[0]
        used = services.ctx.db.query(
            "SELECT COALESCE(SUM(byte_count),0) AS bytes FROM artifacts WHERE namespace='media'"
        )[0]["bytes"]
        return {
            "files": [_record_from_row(r).to_dict() for r in rows],
            "bytes": total["bytes"],
            "count": total["count"],
            "used_bytes": used,
            "quota_bytes": limit("quota_bytes", 10 * 1024**3),
            "next_offset": offset + len(rows) if offset + len(rows) < total["count"] else None,
        }

    def support(params):
        record = services.sessions.show(params["session_id"])
        return {"vision": _caller_for(services, record).capabilities().vision is True}

    def receive(params):
        session = services.sessions.show(params["session_id"])
        try:
            source = Path(params["path"])
            if source.is_symlink() or not source.is_file():
                raise ValueError("Invalid image file")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            ImageReference("", source, digest, "").encoded()
            from rinari.artifacts.limits import limit

            record = import_file(
                services.artifacts,
                session.id,
                source,
                expected_hash=digest,
                maximum=limit("image_bytes", 10 * 1024**2),
                provenance="owner-channel-image",
            )
            if record.sha256 != digest:
                raise ValueError("Image changed during import")
        except (ValueError, OSError) as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        ref = {"uri": record.uri(), "sha256": record.sha256}
        return {"artifact": record.to_dict(), "attachment": ref}

    def delete(params):
        record = services.artifacts.meta(params["uri"])
        if record.namespace != "media":
            raise EngineProtocolError(INVALID_PARAMS, "Only imported media can be removed here")
        services.artifacts._storage_path(record.storage_path).unlink(missing_ok=True)
        services.ctx.db.execute(
            "DELETE FROM artifacts WHERE session_ref=? AND namespace=? AND id=?",
            (record.session_ref, record.namespace, record.id),
        )
        return {"deleted": True}

    dispatcher.register("session.image_support", support)
    dispatcher.register("artifact.receive_image", receive)
    dispatcher.register("artifact.delete_media", delete)
    dispatcher.register("artifact.media_list", listing)
    dispatcher.register("model.vision.set", configure_vision)
