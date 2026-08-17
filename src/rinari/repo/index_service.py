"""Index service: thin application wrapper over the repository-index core.

Business rules (phase 3 Repository Index):
- the index is project-scoped by canonical root (harness.md 98);
- build is incremental by default, rebuild is a full reparse (harness.md 99:
  never rebuild everything just because a single file changed);
- the semantic layer stays declared as 'none' unless explicitly enabled
  (stack.md 68: optional add-on, never the default).
"""

from __future__ import annotations

from pathlib import Path

from rinari.application.context import AppContext
from rinari.repo.index import (
    SEMANTIC_LAYER_NONE,
    IndexBuildResult,
    build_index,
    doctor_index,
    query_index,
)
from rinari.shared.clock import now_iso
from rinari.shared.errors import InvalidUsageError


class IndexService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def repo(self):
        return self._ctx.index_repo

    def _root(self, path: str | Path) -> Path:
        root = Path(path).expanduser()
        if not root.exists():
            raise InvalidUsageError(
                f"Path does not exist: {root}",
                hint="Point `rinari index` at an existing project directory.",
            )
        resolved = root.resolve()
        if not resolved.is_dir():
            raise InvalidUsageError(
                f"Not a directory: {resolved}",
                hint="`rinari index` operates on a project directory.",
            )
        return resolved

    def status(self, path: str | Path) -> dict:
        project_root = str(self._root(path))
        meta = self._ctx.index_repo.get_meta(project_root)
        if meta is None:
            return {"project_root": project_root, "indexed": False, "meta": None}
        return {
            "project_root": project_root,
            "indexed": True,
            "semantic_layer": meta["semantic_layer"],
            "meta": {
                "files": meta["files"],
                "symbols": meta["symbols"],
                "references": meta["ref_count"],
                "built_at": meta["built_at"],
                "updated_at": meta["updated_at"],
            },
        }

    def build(self, path: str | Path) -> IndexBuildResult:
        root = self._root(path)
        project_root = str(root)
        now = now_iso(self._ctx.clock)
        meta = self._ctx.index_repo.get_meta(project_root)
        built_at = (meta or {}).get("built_at") or now
        return build_index(
            self._ctx.index_repo, root, built_at=built_at, updated_at=now, full=False
        )

    def update(self, path: str | Path) -> IndexBuildResult:
        return self.build(path)

    def rebuild(self, path: str | Path) -> IndexBuildResult:
        root = self._root(path)
        now = now_iso(self._ctx.clock)
        meta = self._ctx.index_repo.get_meta(str(root))
        built_at = (meta or {}).get("built_at") or now
        return build_index(
            self._ctx.index_repo,
            root,
            built_at=built_at,
            updated_at=now,
            full=True,
        )

    def clear(self, path: str | Path) -> bool:
        return self._ctx.index_repo.clear(str(self._root(path)))

    def search(self, path: str | Path, query: str, *, limit: int = 50) -> dict:
        project_root = str(self._root(path))
        if self._ctx.index_repo.get_meta(project_root) is None:
            raise InvalidUsageError(
                f"Project is not indexed: {project_root}",
                hint=f"Run `rinari index build {path}` first.",
            )
        return query_index(self._ctx.index_repo, project_root, query, limit=limit)

    def doctor(self, path: str | Path) -> dict:
        return doctor_index(self._ctx.index_repo, self._root(path))

    @staticmethod
    def semantic_layer() -> str:
        return SEMANTIC_LAYER_NONE
