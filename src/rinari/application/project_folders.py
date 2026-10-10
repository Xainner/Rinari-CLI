"""A project's working folders: the primary one plus any others it owns.

The primary folder is `projects.canonical_root` (it still keys memory,
tasks, checkpoints and grants). Extra folders are full working folders only
when trusted (the policy and the sandbox read them from `working_folders`);
an untrusted one is listed but stays outside the project until trusted.
A folder belongs to one project only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.shared.clock import now_iso

MAX_FOLDERS = 16

#: Validation codes the desktop explains to the owner.
NOT_FOUND = "NOT_FOUND"
NOT_DIRECTORY = "NOT_DIRECTORY"
HOME = "HOME"
ENGINE_HOME = "ENGINE_HOME"
DUPLICATE = "DUPLICATE"
NESTED = "NESTED"
IN_PROJECT = "IN_PROJECT"
TOO_MANY = "TOO_MANY"


@dataclass(frozen=True, slots=True)
class FolderCheck:
    input: str
    canonical_path: str | None
    ok: bool
    code: str | None = None
    message: str = ""
    project_id: str | None = None
    project_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        error = None
        if not self.ok:
            error = {
                "code": self.code,
                "message": self.message,
                "project_id": self.project_id,
                "project_name": self.project_name,
            }
        return {
            "input": self.input,
            "canonical_path": self.canonical_path,
            "ok": self.ok,
            "error": error,
        }


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(child: str, parent: str) -> bool:
    child_key, parent_key = _key(child), _key(parent)
    return child_key == parent_key or child_key.startswith(parent_key.rstrip("\\/") + os.sep)


class ProjectFolders:
    def __init__(self, ctx, user_home: Path | None = None) -> None:
        self._ctx = ctx
        self._user_home = (user_home or Path.home()).expanduser().resolve()

    # -- reads -----------------------------------------------------------------------

    def list(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._ctx.db.query(
            "SELECT canonical_path, position, label FROM project_folders WHERE project_id = ? "
            "ORDER BY position",
            (project_id,),
        )
        if not rows:
            project = self._ctx.project_repo.get(project_id)
            if project is None:
                return []
            return [{"path": project.canonical_root, "primary": True, "position": 0, "label": ""}]
        return [
            {
                "path": row["canonical_path"],
                "primary": int(row["position"]) == 0,
                "position": int(row["position"]),
                "label": row["label"] or "",
            }
            for row in rows
        ]

    def owner_of(self, path: str | Path) -> str | None:
        """The project whose folder contains `path` (longest match), if any."""
        candidate = str(Path(path).expanduser().resolve())
        best: tuple[int, str] | None = None
        for row in self._ctx.db.query("SELECT project_id, canonical_path FROM project_folders"):
            folder = str(row["canonical_path"])
            if _inside(candidate, folder) and (best is None or len(folder) > best[0]):
                best = (len(folder), str(row["project_id"]))
        return best[1] if best else None

    def exact_owner(self, path: str | Path) -> str | None:
        candidate = _key(str(Path(path).expanduser().resolve()))
        for row in self._ctx.db.query("SELECT project_id, canonical_path FROM project_folders"):
            if _key(str(row["canonical_path"])) == candidate:
                return str(row["project_id"])
        return None

    # -- validation ----------------------------------------------------------------------

    def validate(self, paths: list[str], project_id: str | None = None) -> list[FolderCheck]:
        """Each path checked on its own and against the others and every project."""
        checks: list[FolderCheck] = []
        existing_own = [f["path"] for f in self.list(project_id)] if project_id else []
        others = [
            (str(row["project_id"]), str(row["canonical_path"]))
            for row in self._ctx.db.query("SELECT project_id, canonical_path FROM project_folders")
            if row["project_id"] != project_id
        ]
        engine_home = str(Path(self._ctx.layout.root).resolve())
        accepted: list[str] = []
        for raw in paths:
            text = raw.strip() if isinstance(raw, str) else ""
            if not text:
                checks.append(FolderCheck(str(raw), None, False, NOT_FOUND, "empty path"))
                continue
            path = Path(text).expanduser()
            if not path.exists():
                checks.append(FolderCheck(text, None, False, NOT_FOUND, "folder not found"))
                continue
            if not path.is_dir():
                checks.append(FolderCheck(text, None, False, NOT_DIRECTORY, "not a folder"))
                continue
            canonical = str(path.resolve())
            if _key(canonical) == _key(str(self._user_home)):
                checks.append(
                    FolderCheck(text, canonical, False, HOME, "the personal folder itself")
                )
                continue
            if _inside(canonical, engine_home):
                checks.append(
                    FolderCheck(text, canonical, False, ENGINE_HOME, "Rinari's own data folder")
                )
                continue
            if any(_key(canonical) == _key(previous) for previous in accepted):
                checks.append(FolderCheck(text, canonical, False, DUPLICATE, "listed twice"))
                continue
            nested = next(
                (
                    previous
                    for previous in [*accepted, *existing_own]
                    if _inside(canonical, previous) or _inside(previous, canonical)
                ),
                None,
            )
            if nested is not None:
                checks.append(
                    FolderCheck(text, canonical, False, NESTED, f"inside or around {nested}")
                )
                continue
            owner = next((pid for pid, folder in others if _key(folder) == _key(canonical)), None)
            if owner is not None:
                project = self._ctx.project_repo.get(owner)
                checks.append(
                    FolderCheck(
                        text,
                        canonical,
                        False,
                        IN_PROJECT,
                        "already a folder of another project",
                        project_id=owner,
                        project_name=project.name if project else None,
                    )
                )
                continue
            if len(accepted) + len(existing_own) >= MAX_FOLDERS:
                checks.append(
                    FolderCheck(text, canonical, False, TOO_MANY, f"at most {MAX_FOLDERS}")
                )
                continue
            accepted.append(canonical)
            checks.append(FolderCheck(text, canonical, True))
        return checks

    # -- writes ------------------------------------------------------------------------------

    def ensure_primary(self, project_id: str, canonical_root: str) -> None:
        self._ctx.db.execute(
            "INSERT OR IGNORE INTO project_folders "
            "(project_id, canonical_path, position, label, added_at) VALUES (?, ?, 0, '', ?)",
            (project_id, canonical_root, now_iso(self._ctx.clock)),
        )

    def add(self, project_id: str, canonical_path: str) -> None:
        row = self._ctx.db.query_one(
            "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM project_folders WHERE project_id = ?",
            (project_id,),
        )
        self._ctx.db.execute(
            "INSERT INTO project_folders (project_id, canonical_path, position, label, added_at) "
            "VALUES (?, ?, ?, '', ?)",
            (project_id, canonical_path, int(row["n"]), now_iso(self._ctx.clock)),
        )

    def remove(self, project_id: str, canonical_path: str) -> bool:
        cursor = self._ctx.db.execute(
            "DELETE FROM project_folders WHERE project_id = ? AND canonical_path = ? "
            "AND position > 0",
            (project_id, canonical_path),
        )
        return cursor.rowcount > 0
