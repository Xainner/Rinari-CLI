"""Project trust: grants, identity fingerprints, and revalidation (phase 3).

Trust is granted per canonical project path. Each grant captures an identity
fingerprint of the project (Git directory identity + remotes for a repository, the
`.rinari/project.toml` marker otherwise) so a later change of identity makes
the entry *need revalidation* instead of silently staying active.
"""

from __future__ import annotations

import contextlib
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
    # Set when this read re-identified the grant on its own: "git-init" when a
    # trusted plain folder became a fresh local repository (plan 0.4.0 #38).
    reidentified: str | None = None


def fingerprint_for(root: Path) -> str:
    """Stable identity digest for a project directory.

    Git repositories use their directory identity and sorted remotes. Commits
    and branch switches are normal work, not changes of project identity.
    Marker-only projects fingerprint the marker file; plain directories
    fall back to their canonical path.
    """
    root = Path(root).expanduser().resolve()
    if (root / ".git").exists():
        directory = _git(root, ["rev-parse", "--absolute-git-dir"]).strip()
        git_dir = Path(directory) if directory else root / ".git"
        info = git_dir.stat()
        identity = f"{git_dir.resolve()}:{info.st_dev}:{info.st_ino}"
        remotes = "\n".join(sorted(_git(root, ["remote", "-v"]).strip().splitlines()))
        return "git-v2:" + _sha256(f"directory={identity}\nremotes={remotes}")
    marker = root / ".rinari" / "project.toml"
    if marker.is_file():
        try:
            return _sha256(marker.read_bytes())
        except OSError:
            pass
    return _sha256(f"dir:{root}")


def _fresh_local_repo(root: Path, previous: str | None, current: str) -> bool:
    """A trusted plain folder (or marker project) that just became a local repo.

    True only when the old grant was the folder's own non-Git identity, the
    `.git` is a directory inside the folder (not a file pointing elsewhere)
    and the repository has no remotes.
    """
    if not previous or not current.startswith("git-v2:"):
        return False
    root = Path(root).expanduser().resolve()
    if not (root / ".git").is_dir():
        return False
    plain = {_sha256(f"dir:{root}")}
    marker = root / ".rinari" / "project.toml"
    if marker.is_file():
        with contextlib.suppress(OSError):
            plain.add(_sha256(marker.read_bytes()))
    if previous not in plain:
        return False
    return not _git(root, ["remote"]).strip()


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
        # Upgrade old grants only when their original identity still matches.
        # An already-invalid grant must still be confirmed by the user.
        if current.startswith("git-v2:") and not (entry.fingerprint or "").startswith("git-v2:"):
            head = _git(raw, ["rev-parse", "HEAD"]).strip()
            remotes = _git(raw, ["remote", "-v"]).strip()
            if entry.fingerprint == _sha256(f"head={head}\nremotes={remotes}"):
                entry.fingerprint = current
                self._ctx.trust_repo.upsert(entry)
        reidentified = None
        if current != entry.fingerprint and _fresh_local_repo(raw, entry.fingerprint, current):
            # `git init` inside a folder that was already trusted changes its
            # fingerprint but not who owns it. Asking again (and only on the
            # next open) is noise. A repository brought from elsewhere (with
            # remotes, or a `.git` file pointing out) is still asked about.
            entry.fingerprint = current
            entry.updated_at = now_iso(self._ctx.clock)
            self._ctx.trust_repo.upsert(entry)
            reidentified = "git-init"
        state = STATE_TRUSTED if current == entry.fingerprint else STATE_REVALIDATION
        return TrustStatus(
            path=str(raw),
            canonical_path=canonical,
            state=state,
            fingerprint=current,
            trusted_at=entry.trusted_at,
            reidentified=reidentified,
        )

    def list(self) -> list[TrustStatus]:
        return [self.status(rec.canonical_path) for rec in self._ctx.trust_repo.list()]

    def is_trusted(self, path: str | Path) -> bool:
        return self.status(path).state == STATE_TRUSTED
