"""Project trust: grants, identity fingerprints, and revalidation (phase 3).

Trust is granted per canonical project path. Each grant captures an identity
fingerprint of the project (Git HEAD + remotes when it is a repository, the
`.rinari/project.toml` marker otherwise) so a later change of identity makes
the entry *need revalidation* instead of silently staying active.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from rinari.application.context import AppContext
from rinari.projects._git_process import capture_git
from rinari.shared.clock import now_iso
from rinari.shared.errors import InvalidUsageError
from rinari.storage.records import TrustEntryRecord

STATE_TRUSTED = "trusted"
STATE_NOT_TRUSTED = "not-trusted"
STATE_REVALIDATION = "revalidation-required"
STATE_NOT_FOUND = "not-found"

_GIT_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class TrustStatus:
    path: str
    canonical_path: str
    state: str
    fingerprint: str | None
    trusted_at: str | None


def fingerprint_for(root: Path) -> str:
    """Stable identity digest for a project directory.

    Git repositories are fingerprinted by HEAD + sorted remotes (so a
    re-cloned copy of a different remote is not the same identity).
    Marker-only projects fingerprint the marker file; plain directories
    fall back to their canonical path.
    """
    root = Path(root).expanduser().resolve()
    if (root / ".git").exists():
        head = _git(root, ["rev-parse", "HEAD"]).strip()
        remotes = _git(root, ["remote", "-v"]).strip()
        return _sha256(f"head={head}\nremotes={remotes}")
    marker = root / ".rinari" / "project.toml"
    if marker.is_file():
        try:
            return _sha256(marker.read_bytes())
        except OSError:
            pass
    return _sha256(f"dir:{root}")


def _git(cwd: Path, args: list[str]) -> str:
    return capture_git(cwd, args, timeout_s=_GIT_TIMEOUT_S) or ""


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


class TrustService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def add(self, path: str | Path) -> TrustEntryRecord:
        root = Path(path).expanduser()
        if not root.exists():
            raise InvalidUsageError(
                f"Path does not exist: {root}",
                hint="Trust a directory that exists; initialize one with `rinari init`.",
            )
        canonical = str(root.resolve())
        now = now_iso(self._ctx.clock)
        record = TrustEntryRecord(
            canonical_path=canonical,
            fingerprint=fingerprint_for(root),
            trusted_at=now,
            updated_at=now,
        )
        self._ctx.trust_repo.upsert(record)
        return self._ctx.trust_repo.get(canonical) or record

    def remove(self, path: str | Path) -> bool:
        return self._ctx.trust_repo.remove(str(Path(path).expanduser().resolve()))

    def status(self, path: str | Path) -> TrustStatus:
        raw = Path(path).expanduser()
        canonical = str(raw.resolve())
        entry = self._ctx.trust_repo.get(canonical)
        if entry is None:
            # There is no identity to compare until the user grants trust.
            # Avoid two Git subprocesses on every prompt in an untrusted
            # project; besides being wasted work, a broken Git installation
            # must not prevent the model request from starting.
            return TrustStatus(
                path=str(raw),
                canonical_path=canonical,
                state=STATE_NOT_TRUSTED,
                fingerprint=None,
                trusted_at=None,
            )
        if not raw.exists():
            return TrustStatus(
                path=str(raw),
                canonical_path=canonical,
                state=STATE_NOT_FOUND,
                fingerprint=entry.fingerprint,
                trusted_at=entry.trusted_at,
            )
        current = fingerprint_for(raw)
        state = STATE_TRUSTED if current == entry.fingerprint else STATE_REVALIDATION
        return TrustStatus(
            path=str(raw),
            canonical_path=canonical,
            state=state,
            fingerprint=current,
            trusted_at=entry.trusted_at,
        )

    def list(self) -> list[TrustStatus]:
        return [self.status(rec.canonical_path) for rec in self._ctx.trust_repo.list()]

    def is_trusted(self, path: str | Path) -> bool:
        return self.status(path).state == STATE_TRUSTED
