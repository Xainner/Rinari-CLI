"""Rinari profiles as workspaces: the active one, and what belongs to each.

A Rinari profile (soul + mode + per-agent models, `ProfileBundleStore`) is
also where projects and conversations live. One profile is active, shared by
the CLI and the desktop (`<home>/active_profile`, like `active_soul`):
new projects and conversations are created in it, and the desktop lists
only its work. The built-in `default` profile always exists and holds
everything created before profiles organized work, so nothing is hidden.

Invariant: a conversation that belongs to a project has the project's
profile. Moving a project moves its conversations with it; moving one of
those conversations alone means taking it out of the project first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.profiles.bundles import (
    DEFAULT_PROFILE_ID,
    PROFILE_ID_RE,
    ProfileBundle,
    ProfileBundleStore,
)
from rinari.shared.errors import InvalidUsageError, NotFoundError

ACTIVE_FILE = "active_profile"


@dataclass(frozen=True, slots=True)
class ProfileMove:
    rinari_profile_id: str
    previous_rinari_profile_id: str
    project_id: str | None
    session_ids: tuple[str, ...]


class RinariProfileService:
    def __init__(self, ctx, store: ProfileBundleStore | None = None) -> None:
        self._ctx = ctx
        self.store = store or ProfileBundleStore(ctx.home)

    # -- active profile ---------------------------------------------------------

    def _active_path(self) -> Path:
        return Path(self._ctx.home) / ACTIVE_FILE

    def active_id(self) -> str:
        try:
            value = self._active_path().read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_PROFILE_ID
        if value and PROFILE_ID_RE.match(value) and self.store.exists(value):
            return value
        return DEFAULT_PROFILE_ID

    def active(self) -> ProfileBundle:
        return self.store.get(self.active_id())

    def activate(self, profile_id: str) -> tuple[str, ProfileBundle]:
        """Make `profile_id` active; returns the previous id and the profile."""
        bundle = self.store.get(profile_id)
        previous = self.active_id()
        path = self._active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bundle.id + "\n", encoding="utf-8")
        return previous, bundle

    def resolve(self, requested: str | None) -> str:
        """The profile for something new: the requested one or the active one."""
        if requested is None or requested == "":
            return self.active_id()
        if not self.store.exists(requested):
            raise NotFoundError(f"Unknown profile: {requested}.")
        return requested

    def for_project(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        project = self._ctx.project_repo.get(project_id)
        return project.rinari_profile_id if project is not None else None

    # -- listing ----------------------------------------------------------------

    def counts(self) -> dict[str, dict[str, int]]:
        """Projects (not archived) and conversations (open ones) per profile."""
        result: dict[str, dict[str, int]] = {}
        for row in self._ctx.db.query(
            "SELECT rinari_profile_id AS id, COUNT(*) AS n FROM projects "
            "WHERE archived = 0 GROUP BY rinari_profile_id"
        ):
            result.setdefault(row["id"], {"projects": 0, "sessions": 0})["projects"] = row["n"]
        for row in self._ctx.db.query(
            "SELECT rinari_profile_id AS id, COUNT(*) AS n FROM sessions "
            "WHERE state NOT IN ('closed', 'archived') GROUP BY rinari_profile_id"
        ):
            result.setdefault(row["id"], {"projects": 0, "sessions": 0})["sessions"] = row["n"]
        return result

    def summaries(self) -> list[dict[str, Any]]:
        active = self.active_id()
        counts = self.counts()
        return [
            {
                **bundle.to_summary(),
                "active": bundle.id == active,
                "counts": counts.get(bundle.id, {"projects": 0, "sessions": 0}),
            }
            for bundle in self.store.list()
        ]

    # -- moving work between profiles -------------------------------------------

    def move_project(self, project_id: str, target: str) -> ProfileMove:
        target = self._known(target)
        project = self._ctx.project_repo.get(project_id)
        if project is None:
            raise NotFoundError(f"Project not found: {project_id}")
        session_ids = tuple(
            record.id
            for record in self._ctx.session_repo.for_project(project.id, project.canonical_root)
        )
        with self._ctx.db.transaction():
            self._ctx.project_repo.set_rinari_profile(project.id, target)
            self._ctx.session_repo.set_rinari_profile(list(session_ids), target)
        return ProfileMove(target, project.rinari_profile_id, project.id, session_ids)

    def move_session(self, session_id: str, target: str) -> ProfileMove:
        """A conversation that is not (or no longer) part of a project."""
        target = self._known(target)
        record = self._ctx.session_repo.get(session_id)
        if record is None:
            raise NotFoundError(f"Session not found: {session_id}")
        if record.project_id:
            raise InvalidUsageError(
                "This conversation belongs to a project: move the project, or take the "
                "conversation out of it first."
            )
        with self._ctx.db.transaction():
            self._ctx.session_repo.set_rinari_profile([record.id], target)
        return ProfileMove(target, record.rinari_profile_id, None, (record.id,))

    def remove(self, profile_id: str, reassign_to: str = DEFAULT_PROFILE_ID) -> dict[str, Any]:
        """Remove a profile; its projects and conversations go to `reassign_to`."""
        if reassign_to == profile_id:
            raise InvalidUsageError("Reassign the work to another profile.")
        target = self._known(reassign_to)
        self.store.get(profile_id)  # NotFound before touching anything
        was_active = self.active_id() == profile_id
        with self._ctx.db.transaction():
            projects = self._ctx.db.execute(
                "UPDATE projects SET rinari_profile_id = ? WHERE rinari_profile_id = ?",
                (target, profile_id),
            ).rowcount
            sessions = self._ctx.db.execute(
                "UPDATE sessions SET rinari_profile_id = ? WHERE rinari_profile_id = ?",
                (target, profile_id),
            ).rowcount
        self.store.remove(profile_id)
        if was_active:
            self.activate(target)
        return {
            "removed": {"id": profile_id},
            "reassigned": {"to": target, "projects": projects, "sessions": sessions},
            "active_id": self.active_id(),
        }

    def reconcile_orphans(self) -> int:
        """Work whose profile file was deleted by hand goes back to the default."""
        known = {bundle.id for bundle in self.store.list()}
        placeholders = ",".join("?" for _ in known)
        moved = 0
        with self._ctx.db.transaction():
            for table in ("projects", "sessions"):
                moved += self._ctx.db.execute(
                    f"UPDATE {table} SET rinari_profile_id = ? "
                    f"WHERE rinari_profile_id NOT IN ({placeholders})",
                    (DEFAULT_PROFILE_ID, *sorted(known)),
                ).rowcount
        return moved

    def _known(self, profile_id: str) -> str:
        if not isinstance(profile_id, str) or not self.store.exists(profile_id):
            raise NotFoundError(f"Unknown profile: {profile_id}.")
        return profile_id
