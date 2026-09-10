"""Private content-addressed snapshots used only by conflict-safe undo."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ChangeBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = (root / "change-blobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self._target(digest)
        if target.is_file():
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="snapshot-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return digest

    def read(self, reference: str) -> bytes:
        content = self._target(reference).read_bytes()
        if hashlib.sha256(content).hexdigest() != reference:
            raise OSError("Private change snapshot failed integrity validation")
        return content

    def _target(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Invalid change blob reference")
        target = (self.root / digest[:2] / digest).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError("Change blob escaped private root")
        return target
