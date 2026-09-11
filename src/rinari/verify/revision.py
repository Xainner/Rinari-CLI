"""Bounded workspace revision for verification freshness (metadata, no secrets)."""

import hashlib
from pathlib import Path

from rinari.repo.search import walk_files

PREFIX = "rinari-workspace-revision:"


def revision(root: Path) -> str | None:
    digest = hashlib.sha256()
    for index, path in enumerate(walk_files(root, max_files=20001)):
        if index >= 20000:
            return None
        if any(
            part in {".rinari", ".pytest_cache", ".coverage"}
            for part in path.relative_to(root).parts
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            return None
        digest.update(f"{path.relative_to(root)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()
