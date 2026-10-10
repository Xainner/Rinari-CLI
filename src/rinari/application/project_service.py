"""Project lifecycle service: marker files, identity upsert, `rinari init`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.application.context import AppContext
from rinari.application.project_folders import FolderCheck, ProjectFolders
from rinari.projects.detector import is_home_root
from rinari.projects.git import git_fingerprint
from rinari.projects.git_head import HEADS
from rinari.shared.clock import now_iso
from rinari.shared.errors import (
    ConflictError,
    InvalidUsageError,
    NotFoundError,
    PermissionDeniedError,
)
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


class ProjectFolderError(InvalidUsageError):
    """Folders that cannot be part of a project; carries the per-folder report."""

    def __init__(self, checks: list[FolderCheck]) -> None:
        failed = [check for check in checks if not check.ok]
        reason = failed[0].message if failed else "invalid folder"
        super().__init__(f"Project folders are not valid: {reason}.")
        self.checks = checks


class ProjectService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        # RinariProfileService, bound by build_services: new projects go to
        # the active profile (or the one asked for).
        self.profiles = None
        self.folders = ProjectFolders(ctx)

    def _profile_for_new(self, requested: str | None) -> str:
        if self.profiles is None:
            return requested or "default"
        return self.profiles.resolve(requested)

    def upsert(self, root: Path, *, rinari_profile_id: str | None = None) -> ProjectRecord:
        canonical = str(Path(root).expanduser().resolve())
        now = now_iso(self._ctx.clock)
        existing = self._ctx.project_repo.get_by_root(canonical)
        if existing is None:
            # Opening one of a project's extra folders opens that project.
            owner_id = self.folders.exact_owner(canonical)
            owner = self._ctx.project_repo.get(owner_id) if owner_id else None
            if owner is not None:
                owner.updated_at = now
                owner.last_opened_at = now
                self._ctx.project_repo.update(owner)
                return owner
        if existing is not None:
            existing.git_fingerprint = git_fingerprint(Path(canonical))
            existing.updated_at = now
            existing.last_opened_at = now
            if not existing.name or existing.name == existing.canonical_root:
                existing.name = Path(canonical).name or canonical
            self._ctx.project_repo.update(existing)
            return existing
        record = ProjectRecord(
            id=self._ctx.ids.new("prj"),
            canonical_root=canonical,
            git_fingerprint=git_fingerprint(Path(canonical)),
            metadata={},
            created_at=now,
            updated_at=now,
            name=Path(canonical).name or canonical,
            last_opened_at=now,
            rinari_profile_id=self._profile_for_new(rinari_profile_id),
        )
        with self._ctx.db.transaction():
            self._ctx.project_repo.insert(record)
            self.folders.ensure_primary(record.id, canonical)
        return record

    # -- several working folders ------------------------------------------------

    def validate_folders(
        self, paths: list[str], project_id: str | None = None
    ) -> list[FolderCheck]:
        return self.folders.validate(paths, project_id)

    def create(
        self,
        *,
        name: str,
        description: str = "",
        folders: list[str],
        rinari_profile_id: str | None = None,
    ) -> ProjectRecord:
        """A project with its working folders (the first is the primary).

        All or nothing: an invalid folder rejects the whole creation with
        the report (ProjectFolderError). Trust is granted by the caller.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise InvalidUsageError("Project name must not be empty.")
        if not folders:
            raise InvalidUsageError("A project needs at least one folder.")
        checks = self.folders.validate(folders)
        if not all(check.ok for check in checks):
            raise ProjectFolderError(checks)
        primary, *extras = [str(check.canonical_path) for check in checks]
        now = self._now()
        record = ProjectRecord(
            id=self._ctx.ids.new("prj"),
            canonical_root=primary,
            git_fingerprint=git_fingerprint(Path(primary)),
            metadata={},
            created_at=now,
            updated_at=now,
            name=clean_name[:120],
            description=(description or "").strip()[:500],
            last_opened_at=now,
            rinari_profile_id=self._profile_for_new(rinari_profile_id),
        )
        with self._ctx.db.transaction():
            self._ctx.project_repo.insert(record)
            self.folders.ensure_primary(record.id, primary)
            for extra in extras:
                self.folders.add(record.id, extra)
        return record

    def add_folder(self, project_id: str, path: str) -> ProjectRecord:
        record = self.get(project_id)
        checks = self.folders.validate([path], record.id)
        if not checks[0].ok:
            raise ProjectFolderError(checks)
        self.folders.add(record.id, str(checks[0].canonical_path))
        return record

    def remove_folder(self, project_id: str, path: str) -> ProjectRecord:
        record = self.get(project_id)
        canonical = str(Path(path).expanduser().resolve())
        if canonical == record.canonical_root:
            raise ConflictError("The primary folder cannot be removed.")
        if not self.folders.remove(record.id, canonical):
            raise NotFoundError(f"Not a folder of this project: {canonical}")
        return record

    def get(self, project_id: str) -> ProjectRecord:
        record = self._ctx.project_repo.get(project_id)
        if record is None:
            raise NotFoundError(f"Project not found: {project_id}")
        return self._normalize(record)

    def list(
        self, *, include_archived: bool = False, rinari_profile_id: str | None = None
    ) -> list[ProjectRecord]:
        return [
            self._normalize(record)
            for record in self._ctx.project_repo.list(
                include_archived=include_archived, rinari_profile_id=rinari_profile_id
            )
        ]

    def add(
        self,
        root: Path,
        *,
        name: str | None = None,
        description: str = "",
        rinari_profile_id: str | None = None,
    ) -> tuple[ProjectRecord, bool]:
        canonical = Path(root).expanduser().resolve()
        if not canonical.is_dir():
            raise NotFoundError(f"Project folder not found: {canonical}")
        existing = self._ctx.project_repo.get_by_root(str(canonical))
        record = self.upsert(canonical, rinari_profile_id=rinari_profile_id)
        changed = False
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise InvalidUsageError("Project name must not be empty.")
            record.name = clean_name[:120]
            changed = True
        if description:
            record.description = description.strip()[:500]
            changed = True
        if record.archived:
            record.archived = False
            changed = True
        if changed:
            record.updated_at = self._now()
            self._ctx.project_repo.update(record)
        return record, existing is None

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> ProjectRecord:
        record = self.get(project_id)
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise InvalidUsageError("Project name must not be empty.")
            record.name = clean_name[:120]
        if description is not None:
            record.description = description.strip()[:500]
        if pinned is not None:
            record.pinned = pinned
        if archived is not None:
            record.archived = archived
        record.updated_at = self._now()
        self._ctx.project_repo.update(record)
        return record

    def archive(self, project_id: str) -> ProjectRecord:
        return self.update(project_id, archived=True, pinned=False)

    def purge(self, project_id: str) -> dict[str, int]:
        """Forget what Rinari keeps about a project, then the project itself.

        The caller removes sessions, checkpoints and scheduled tasks first:
        those carry runtime state and files of their own. What is left is keyed
        by the project root (memory, tasks, verification, repository index,
        trust) and goes here in one transaction. The folder is never touched.
        """
        record = self.get(project_id)
        root = record.canonical_root
        removed: dict[str, int] = {}
        with self._ctx.db.transaction() as db:
            for table, column, key in (
                ("project_memory", "project_root", root),
                ("episodic_memory", "project_root", root),
                ("tasks", "project_root", root),
                ("validation_records", "project_root", root),
                ("repo_index_files", "project_root", root),
                ("repo_index_symbols", "project_root", root),
                ("repo_index_references", "project_root", root),
                ("repo_index_test_map", "project_root", root),
                ("repo_index_meta", "project_root", root),
                ("trust_entries", "canonical_path", root),
                ("network_rules", "project_id", record.id),
            ):
                removed[table] = db.execute(
                    f"DELETE FROM {table} WHERE {column} = ?", (key,)
                ).rowcount
            db.execute("DELETE FROM projects WHERE id = ?", (record.id,))
        return removed

    def _normalize(self, record: ProjectRecord) -> ProjectRecord:
        if record.name and record.name != record.canonical_root:
            return record
        record.name = Path(record.canonical_root).name or record.canonical_root
        record.last_opened_at = record.last_opened_at or record.updated_at
        self._ctx.project_repo.update(record)
        return record

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

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

    def list_recent(
        self, limit: int = 20, *, rinari_profile_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Projects ordered by real open activity (shared session table).

        No second store: recency derives from session last_active_at, so
        CLI and desktop activity both count. Roots never opened in a
        session fall back to the project record recency. Timestamps are
        ISO-8601 from the same clock, so string order is chronological.
        """
        # Deferred import: session_service imports this module.
        from rinari.application.session_service import SESSION_STATE_ACTIVE

        limit = max(1, min(int(limit), 100))
        activity: dict[str, str] = {}
        bound: dict[str, str] = {}
        for session in self._ctx.session_repo.list(limit=500):
            root = session.project_root_snapshot
            if not root:
                continue
            activity.setdefault(root, session.last_active_at)
            if session.state == SESSION_STATE_ACTIVE and root not in bound:
                bound[root] = session.id
        ranked = sorted(
            self.list(include_archived=False, rinari_profile_id=rinari_profile_id),
            key=lambda p: activity.get(p.canonical_root, p.updated_at),
            reverse=True,
        )
        return [
            {
                "id": project.id,
                "root": project.canonical_root,
                "name": project.name,
                "description": project.description,
                "pinned": project.pinned,
                "archived": project.archived,
                "git_fingerprint": project.git_fingerprint,
                "last_opened_at": activity.get(
                    project.canonical_root,
                    project.last_opened_at or project.updated_at,
                ),
                "active_session_id": bound.get(project.canonical_root),
                "rinari_profile_id": project.rinari_profile_id,
                "git_head": HEADS.get(project.canonical_root).as_dict(),
            }
            for project in ranked[:limit]
        ]
