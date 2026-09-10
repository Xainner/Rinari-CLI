"""Filesystem sandbox: technical enforcement of granted roots.

The policy engine decides WHAT is allowed; the sandbox enforces it at
execution time. Paths are canonicalized (absolute + symlink-resolved)
before any comparison so symlink/traversal tricks cannot escape a root
(harness.md section 75, AGENTS.md section 13).
"""

from __future__ import annotations

from pathlib import Path

from rinari.shared.errors import SandboxViolationError


class FilesystemSandbox:
    """Enforces the roots granted by policy; the $HOME invariant itself is
    owned by the Policy Engine (a $HOME root only exists via an explicit
    full-access choice or a CHAT cwd the user positioned themselves in)."""

    def __init__(
        self,
        read_root: Path | None,
        write_roots: tuple[Path, ...] = (),
        *,
        unrestricted: bool = False,
        approved_read_roots: tuple[Path, ...] = (),
        unrestricted_reads: bool = False,
    ) -> None:
        self._read_root = read_root.resolve() if read_root else None
        self._write_roots = tuple(r.resolve() for r in write_roots)
        self._unrestricted = unrestricted
        self.unrestricted_reads = unrestricted_reads
        self.approved_read_roots = tuple(r.resolve() for r in approved_read_roots)

    @property
    def read_root(self) -> Path | None:
        return self._read_root

    @property
    def write_roots(self) -> tuple[Path, ...]:
        return self._write_roots

    @property
    def unrestricted(self) -> bool:
        return self._unrestricted

    def resolve(self, path: str | Path, base: Path | None = None) -> Path:
        """Canonicalize a model/user-supplied path against `base`."""
        if base is None:
            raise SandboxViolationError(
                "Cannot resolve a path without a base directory",
                hint="The session has no working directory.",
            )
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = base / candidate
        return candidate.resolve()

    def assert_readable(self, resolved: Path) -> None:
        resolved = resolved.resolve()
        if self._unrestricted or self.unrestricted_reads:
            return
        if any(resolved == root or root in resolved.parents for root in self.approved_read_roots):
            return
        if self._read_root is None:
            raise SandboxViolationError(
                "No read root granted for this session",
                hint="Run inside a project or choose a permission profile.",
            )
        if not (resolved == self._read_root or self._read_root in resolved.parents):
            raise SandboxViolationError(
                f"Path is outside the readable root ({resolved})",
                hint=f"Readable root: {self._read_root}",
            )

    def assert_writable(self, resolved: Path) -> None:
        if self._unrestricted:
            return
        if not self._write_roots:
            raise SandboxViolationError(
                "No write root granted for this session",
                hint="Use a workspace or full-access profile.",
            )
        for root in self._write_roots:
            if resolved == root or root in resolved.parents:
                return
        raise SandboxViolationError(
            f"Path is outside every writable root ({resolved})",
            hint=f"Writable roots: {', '.join(str(r) for r in self._write_roots)}",
        )


class ProcessLimits:
    """Technical process limits applied by the shell tool."""

    def __init__(self, timeout_s: float = 300.0, max_output_bytes: int = 1_000_000) -> None:
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
