"""Host-only media operations. Model access remains behind runtime tools."""

import base64
import hashlib
import io
from pathlib import Path

from rinari.artifacts.attachments import AttachmentPreparationJobs, prepare_attachments
from rinari.artifacts.transfer import import_file
from rinari.cli.agent_runtime import _caller_for
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.models.images import ImageReference


def register_media(dispatcher, services):
    preparation_jobs = AttachmentPreparationJobs(services.artifacts)

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
        vision = _caller_for(services, record).capabilities().vision
        return {"vision": vision, "known": vision is not None}

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

    def prepare(params):
        session_id = params.get("session_id")
        attachments = params.get("attachments", [])
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'session_id' must be a non-empty string."
            )
        services.sessions.show(session_id)
        try:
            prepared = prepare_attachments(services.artifacts, session_id, attachments)
        except (OSError, ValueError, RuntimeError) as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        return {"attachments": [item.reference() for item in prepared]}

    def preview(params):
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'uri' must be a non-empty string.")
        record = services.artifacts.meta(uri)
        if record.namespace not in {"media", "derived"}:
            raise EngineProtocolError(INVALID_PARAMS, "Only attachment artifacts can be previewed")
        max_bytes = params.get("max_bytes") or 512 * 1024
        if type(max_bytes) is not int or max_bytes < 1:
            raise EngineProtocolError(INVALID_PARAMS, "max_bytes must be a positive integer")
        max_bytes = min(max_bytes, 512 * 1024)
        path = services.artifacts._storage_path(record.storage_path)
        if record.content_type in {"image/png", "image/jpeg", "image/webp"}:
            from PIL import Image, ImageOps

            ImageReference(uri, path, record.sha256, record.content_type).encoded()
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                try:
                    image.thumbnail((512, 512))
                    while True:
                        buffer = io.BytesIO()
                        image.save(buffer, format="JPEG", quality=80)
                        thumbnail = buffer.getvalue()
                        if len(thumbnail) <= max_bytes:
                            break
                        if max(image.size) < 32:
                            raise EngineProtocolError(
                                INVALID_PARAMS, "Preview byte limit is too small for an image"
                            )
                        image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)))
                    encoded = base64.b64encode(thumbnail).decode("ascii")
                    return {
                        "artifact": record.to_dict(),
                        "mime_type": "image/jpeg",
                        "base64": encoded,
                        "data_url": f"data:image/jpeg;base64,{encoded}",
                        "width": image.width,
                        "height": image.height,
                        "truncated": False,
                    }
                finally:
                    image.close()
        if not record.content_type.startswith("text/") and record.namespace != "derived":
            candidates = [
                entry
                for entry in services.artifacts.list(session_id=record.session_ref)
                if entry.namespace == "derived"
                and entry.name.startswith(record.sha256)
                and entry.content_type.startswith("text/")
            ]
            if not candidates:
                raise EngineProtocolError(
                    INVALID_PARAMS, "Prepare this document before requesting its text preview"
                )
            record = candidates[-1]
            path = services.artifacts._storage_path(record.storage_path)
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        clipped = content[:max_bytes]
        if record.content_type.startswith("text/") or record.namespace == "derived":
            return {
                "artifact": record.to_dict(),
                "text": clipped.decode("utf-8", errors="replace"),
                "truncated": len(content) > len(clipped),
            }
        raise EngineProtocolError(INVALID_PARAMS, "Unsupported attachment preview")

    def prepare_start(params):
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'session_id' must be a non-empty string."
            )
        services.sessions.show(session_id)
        try:
            return preparation_jobs.start(session_id, params.get("attachments", []))
        except (OSError, ValueError, RuntimeError) as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc

    def prepare_get(params):
        try:
            return preparation_jobs.get(params.get("job_id"))
        except ValueError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc

    def prepare_cancel(params):
        try:
            return preparation_jobs.cancel(params.get("job_id"))
        except ValueError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc

    dispatcher.register("session.image_support", support)
    dispatcher.register("artifact.receive_image", receive)
    dispatcher.register("artifact.delete_media", delete)
    dispatcher.register("artifact.media_list", listing)
    dispatcher.register("attachment.prepare", prepare)
    dispatcher.register("attachment.preview", preview)
    dispatcher.register("attachment.prepare.start", prepare_start)
    dispatcher.register("attachment.prepare.get", prepare_get)
    dispatcher.register("attachment.prepare.cancel", prepare_cancel)
    dispatcher.register("model.vision.set", configure_vision)
    return preparation_jobs
