"""Atomic replacement with optional optimistic content preconditions."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path


class ContentConflict(ValueError):
    pass


def replace_text(path: Path, text: str, expected_hash: str | None = None) -> str:
    data = text.encode("utf-8")

    def check():
        if expected_hash is not None:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
            if current != expected_hash:
                raise ContentConflict(
                    "File changed since it was read; read it again before editing"
                )

    check()
    descriptor, name = tempfile.mkstemp(prefix=".rinari-write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        check()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()
