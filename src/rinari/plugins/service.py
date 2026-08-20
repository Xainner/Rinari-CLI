"""PluginService: lifecycle (install/remove/enable/disable/update) + load.

Application-level facade over the `plugins` table, the manifest loader and
the trust gate. User plugins live under `~/.rinari/plugins/`; project
plugins under `<root>/.rinari/plugins/` and require project trust.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.trust import TrustService

from .loader import LoadedPlugin, load_plugin
from .manifest import PluginManifest, load_manifest


def _now(ctx: AppContext) -> str:
    return now_iso(ctx.clock)


def _manifest_json(manifest: PluginManifest) -> str:
    return json.dumps(
        {
            "name": manifest.name,
            "version": manifest.version,
            "entrypoint": manifest.entrypoint,
            "description": manifest.description,
            "capabilities": list(manifest.capabilities),
        },
        sort_keys=True,
    )


class PluginService:
    def __init__(self, ctx: AppContext, trust: TrustService) -> None:
        self._ctx = ctx
        self._trust = trust

    # -- locations -----------------------------------------------------------

    def user_root(self) -> Path:
        return self._ctx.layout.dir("plugins")

    def project_root(self, root: Path) -> Path:
        return Path(root) / ".rinari" / "plugins"

    def root_for(self, source: str, project: Path | None = None) -> Path:
        if source == "user":
            return self.user_root()
        if source == "project":
            if project is None:
                raise ValueError("project plugins need a project root")
            return self.project_root(project)
        raise ValueError(f"unknown plugin source: {source!r}")

    def _scope(self, source: str) -> str:
        return "global" if source == "user" else "project"

    # -- install / update / remove ------------------------------------------

    def install(self, path: Path, source: str = "user", project: Path | None = None) -> dict:
        src = Path(path).expanduser().resolve()
        manifest = load_manifest(src)
        dest_root = self.root_for(source, project)
        dest = (dest_root / manifest.name).resolve()
        if dest.exists():
            raise PermissionError(
                f"plugin already installed: {manifest.name}",
            )
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, symlinks=False)
        scope = self._scope(source)
        row = self._ctx.plugin_repo.add(
            self._ctx.ids.new("plg"),
            name=manifest.name,
            version=manifest.version,
            source=source,
            scope=scope,
            path=str(dest),
            manifest_json=_manifest_json(manifest),
            created_at=_now(self._ctx),
        )
        if source == "project":
            status = self._trust.status(project).state if project is not None else "not-trusted"
            if status != "trusted":
                row["trusted"] = False
            else:
                row["trusted"] = True
        return row

    def update(
        self, path: Path, name: str, source: str = "user", project: Path | None = None
    ) -> dict:
        scope = self._scope(source)
        row = self._ctx.plugin_repo.find(name, scope)
        if row is None:
            raise LookupError(f"plugin not installed: {name}")
        src = Path(path).expanduser().resolve()
        manifest = load_manifest(src)
        if manifest.name != name:
            raise ValueError(f"manifest name {manifest.name!r} does not match plugin {name!r}")
        dest = Path(row["path"])
        shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=False)
        row = self._ctx.plugin_repo.add(
            row["id"],
            name=manifest.name,
            version=manifest.version,
            source=source,
            scope=scope,
            path=str(dest),
            manifest_json=_manifest_json(manifest),
            created_at=_now(self._ctx),
        )
        return row

    def remove(self, name: str, source: str = "user", project: Path | None = None) -> bool:
        scope = self._scope(source)
        row = self._ctx.plugin_repo.find(name, scope)
        if row is None:
            return False
        path = Path(row["path"])
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        return self._ctx.plugin_repo.delete(name, scope)

    def enable(self, name: str, source: str = "user") -> dict | None:
        scope = self._scope(source)
        ok = self._ctx.plugin_repo.set_enabled(name, scope, True, _now(self._ctx))
        return self._ctx.plugin_repo.find(name, scope) if ok else None

    def disable(self, name: str, source: str = "user") -> dict | None:
        scope = self._scope(source)
        ok = self._ctx.plugin_repo.set_enabled(name, scope, False, _now(self._ctx))
        return self._ctx.plugin_repo.find(name, scope) if ok else None

    # -- introspection ---------------------------------------------------------

    def list(self) -> list[dict]:
        rows = self._ctx.plugin_repo.list()
        for row in rows:
            try:
                row["capabilities"] = json.loads(row["manifest_json"]).get("capabilities", [])
            except (json.JSONDecodeError, TypeError):
                row["capabilities"] = []
        return rows

    def show(self, name: str, source: str = "user") -> dict | None:
        row = self._ctx.plugin_repo.find(name, self._scope(source))
        if row is None:
            return None
        try:
            row["manifest"] = json.loads(row["manifest_json"])
        except (json.JSONDecodeError, TypeError):
            row["manifest"] = None
        return row

    def permissions(self, name: str, source: str = "user") -> dict | None:
        """Preview the requested capabilities before load (install preview)."""
        row = self.show(name, source)
        if row is None:
            return None
        return {
            "name": name,
            "capabilities": row.get("manifest", {}).get("capabilities", []),
            "enabled": bool(row.get("enabled")),
        }

    # -- load (runtime) ----------------------------------------------------------

    def load_all(self, project: Path | None = None) -> list[LoadedPlugin]:
        loaded: list[LoadedPlugin] = []
        for row in self.list():
            if not row.get("enabled"):
                continue
            source = row["source"]
            path = Path(row["path"])
            if source == "project":
                trusted = bool(project is not None and self._trust.is_trusted(project))
            else:
                trusted = True
            try:
                manifest = load_manifest(path)
            except Exception as exc:  # manifest gone or corrupt -> diagnostic only
                from .manifest import PluginError

                code = exc.code if isinstance(exc, PluginError) else "MANIFEST_INVALID"
                plugin = LoadedPlugin(
                    manifest=None,  # type: ignore[arg-type]
                    path=path,
                    source=source,
                    diagnostics=[{"code": code, "message": str(exc)}],
                )
                loaded.append(plugin)
                continue
            loaded.append(load_plugin(manifest, path, source, project_trusted=trusted))
        return loaded

    # -- diagnostics (doctor) ------------------------------------------------------

    def doctor(self, project: Path | None = None) -> list[dict]:
        reports: list[dict] = []
        for row in self.list():
            name, source, path = row["name"], row["source"], Path(row["path"])
            entries: list[dict] = []
            if not path.is_dir():
                entries.append({"code": "PATH_MISSING", "message": str(path)})
            else:
                try:
                    manifest = load_manifest(path)
                    trusted = source != "project" or (
                        project is not None and self._trust.is_trusted(project)
                    )
                    plugin = load_plugin(manifest, path, source, project_trusted=trusted)
                    entries.extend(plugin.diagnostics)
                except Exception as exc:  # pragma: no cover - defensive
                    entries.append({"code": "MANIFEST_INVALID", "message": str(exc)})
            if not entries:
                entries.append({"code": "OK", "message": "plugin loads cleanly"})
            reports.append({"name": name, "source": source, "diagnostics": entries})
        return reports


__all__ = ["PluginService"]
