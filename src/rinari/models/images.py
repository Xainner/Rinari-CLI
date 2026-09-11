"""Validated image references; encoded bytes exist only at the provider boundary."""

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from rinari.artifacts.limits import limit


@dataclass(frozen=True, slots=True)
class ImageReference:
    uri: str
    path: Path
    sha256: str
    mime: str

    def encoded(self):
        from PIL import Image, ImageOps

        if self.path.stat().st_size > limit("image_bytes", 10 * 1024**2):
            raise ValueError("Image exceeds byte limit")
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("Image changed")
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ("JPEG", "PNG", "WEBP") or getattr(source, "n_frames", 1) != 1:
                raise ValueError("Unsupported image format")
            if source.width * source.height > limit("image_pixels", 40_000_000):
                raise ValueError("Image exceeds pixel limit")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2048, 2048))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90)
        return base64.b64encode(output.getvalue()).decode("ascii")


def references(store, session_id, attachments):
    if not isinstance(attachments, list) or len(attachments) > limit("images_per_request", 4):
        raise ValueError("At most four images per request")
    result = []
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {"uri", "sha256"}:
            raise ValueError("Invalid image reference")
        record = store.meta(item["uri"])
        if record.session_ref != session_id or record.sha256 != item["sha256"]:
            raise ValueError("Image reference is outside this session or changed")
        if record.content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError("Artifact is not an image")
        ref = ImageReference(
            record.uri(),
            store._storage_path(record.storage_path),
            record.sha256,
            record.content_type,
        )
        ref.encoded()  # Validate before admitting the request; originals remain untouched.
        result.append(ref)
    return tuple(result)
