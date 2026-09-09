"""Project lifecycle service: marker files, identity upsert, `rinari init`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.application.context import AppContext
from rinari.projects.detector import is_home_root
from rinari.projects.git import git_fingerprint
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, PermissionDeniedError
from rinari.storage.records import ProjectRecord

PROJECT_TOML_HEADER = "# Rinari project marker.\n"

PROJECT_CONFIG_TEMPLATE = (
    "# Rinari project configuration.\n"
    "# These values override user config when the project is trusted.\n"
)

RINARI_MD_TEMPLATE = """# RINARI.md — Project Instructions

Stable instructions for Rinari in this repository.

- State conventions (tests, lint, formatting) and how to run them.
- Note constraints that must always be respected.
- Keep it short; per-task context belongs in the conversation.
"""


class ProjectService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def upsert(self, root: Path) -> ProjectRecord:
        canonical = str(Path(root).expanduser().resolve())
        now = now_iso(self._ctx.clock)
        existing = self._ctx.project_repo.get_by_root(canonical)
        if existing is not None:
            existing.git_fingerprint = git_fingerprint(Path(canonical))
            existing.updated_at = now
            self._ctx.project_repo.update(existing)
            return existing
        record = ProjectRecord(
            id=self._ctx.ids.new("prj"),
            canonical_root=canonical,
            git_fingerprint=git_fingerprint(Path(canonical)),
            metadata={},
            created_at=now,
            updated_at=now,
        )
        self._ctx.project_repo.insert(record)
        return record

    def init(
        self, path: Path, user_home: Path, force: bool = False
    ) -> tuple[ProjectRecord, list[str]]:
        """Create `.rinari/` project files and the project record.

        Returns the record plus a list of created/updated file paths.
        ``force`` rewrites `.rinari/project.toml` and `RINARI.md`.
        """
        root = Path(path).expanduser().resolve()
        if is_home_root(root, user_home):
            raise PermissionDeniedError(
                "$HOME is never an implicit project workspace",
                hint="Run `rinari init <path>` inside a project subdirectory.",
            )
        marker_dir = root / ".rinari"
        project_toml = marker_dir / "project.toml"
        if project_toml.is_file() and not force:
            raise ConflictError(
                f"Project already initialized at {root}",
                hint="Use --force to re-init (existing files are overwritten).",
            )
        root.mkdir(parents=True, exist_ok=True)
        marker_dir.mkdir(parents=True, exist_ok=True)

        created: list[str] = []
        now_iso_str = now_iso(self._ctx.clock)
        if not project_toml.is_file() or force:
            project_toml.write_text(
                f"{PROJECT_TOML_HEADER}name = {root.name!r}\ncreated_at = {now_iso_str!r}\n",
                encoding="utf-8",
            )
            created.append(str(project_toml))
        project_config = marker_dir / "config.toml"
        if not project_config.is_file():
            project_config.write_text(PROJECT_CONFIG_TEMPLATE, encoding="utf-8")
            created.append(str(project_config))
        rinari_md = root / "RINARI.md"
        if not rinari_md.is_file() or force:
            rinari_md.write_text(RINARI_MD_TEMPLATE, encoding="utf-8")
            created.append(str(rinari_md))

        record = self.upsert(root)
        return record, created

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Projects ordered by real open activity (shared session table).

        No second store: recency derives from session last_active_at, so
        CLI and desktop activity both count. Roots never opened in a
        session fall back to the project record recency. Timestamps are
        ISO-8601 from the same clock, so string order is chronological.
        """
        # Deferred import: session_service imports this module.
        from rinari.application.session_service import SESSION_STATE_CLOSED

        limit = max(1, min(int(limit), 100))
        activity: dict[str, str] = {}
        bound: dict[str, str] = {}
        for session in self._ctx.session_repo.list(limit=500):
            root = session.project_root_snapshot
            if not root:
                continue
            activity.setdefault(root, session.last_active_at)
            if session.state != SESSION_STATE_CLOSED and root not in bound:
                bound[root] = session.id
        ranked = sorted(
            self._ctx.project_repo.list(),
            key=lambda p: activity.get(p.canonical_root, p.updated_at),
            reverse=True,
        )
        return [
            {
                "id": project.id,
                "root": project.canonical_root,
                "git_fingerprint": project.git_fingerprint,
                "last_opened_at": activity.get(project.canonical_root, project.updated_at),
                "active_session_id": bound.get(project.canonical_root),
            }
            for project in ranked[:limit]
        ]
