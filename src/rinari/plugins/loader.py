"""Plugin loading: import the entrypoint and run its contribute() (phase 5).

The loader is deliberately narrow:

- only `user` and `project` plugins load; project plugins require project
  trust (checked by the service before calling load; the loader re-checks).
- `contribute(api)` runs with a PluginAPI that only exposes the contribution
  types the manifest *requested*; unrequested types raise.
- plugin tools must be named `<plugin>.<tool>`.
- any exception inside contribute becomes a diagnostic, never a crash.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rinari.plugins.manifest import SUPPORTED_CONTRIBUTIONS, PluginError, PluginManifest
from rinari.tools.definition import ToolDefinition


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    path: Path
    source: str
    tools: list[ToolDefinition] = field(default_factory=list)
    hooks: list[tuple[str, object]] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)

    def ok(self) -> bool:
        return not any(d["code"] == "LOAD_FAILED" for d in self.diagnostics)


class PluginAPI:
    """The surface a plugin's contribute() sees."""

    def __init__(self, plugin_name: str, manifest: PluginManifest) -> None:
        self._plugin_name = plugin_name
        self._manifest = manifest
        self._tools: list[ToolDefinition] = []
        self._hooks: list[tuple[str, object]] = []

    # -- contributions ---------------------------------------------------

    def add(self, kind: str) -> PluginAPI:
        """Return a scoped builder for `kind`; raises if not requested."""
        if kind not in SUPPORTED_CONTRIBUTIONS:
            raise PluginError(
                "UNSUPPORTED_CONTRIBUTION",
                f"contribution type {kind!r} is not supported yet (first pass: "
                f"{', '.join(SUPPORTED_CONTRIBUTIONS)})",
            )
        if not self._manifest.requests(kind):
            raise PluginError(
                "UNSUPPORTED_CONTRIBUTION",
                f"plugin does not request the {kind!r} capability in its manifest",
            )
        return self

    def add_tool(self, tool: ToolDefinition) -> ToolDefinition:
        self.add("tools")
        prefix = f"{self._plugin_name}."
        if not tool.name.startswith(prefix):
            raise PluginError(
                "LOAD_FAILED",
                f"plugin tool {tool.name!r} must be namespaced as {self._plugin_name}.<tool>",
            )
        # Rebind namespace for manifest/capability grouping.
        self._tools.append(tool)
        return tool

    def add_hook(self, event: str, handler: object) -> None:
        self.add("hooks")
        self._hooks.append((event, handler))

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    @property
    def hooks(self) -> list[tuple[str, object]]:
        return list(self._hooks)


def load_plugin(
    manifest: PluginManifest, path: Path, source: str, project_trusted: bool
) -> LoadedPlugin:
    plugin = LoadedPlugin(manifest=manifest, path=Path(path), source=source)
    if source == "project" and not project_trusted:
        plugin.diagnostics.append(
            {"code": "TRUST_REQUIRED", "message": "project plugin requires project trust"}
        )
        return plugin
    entry = Path(path) / manifest.entrypoint
    if not entry.is_file():
        plugin.diagnostics.append(
            {"code": "ENTRYPOINT_MISSING", "message": f"missing entrypoint {entry}"}
        )
        return plugin
    module_name = f"_rinari_plugin_{manifest.name}_{uuid.uuid4().hex[:8]}"
    api = PluginAPI(manifest.name, manifest)
    try:
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            raise PluginError("LOAD_FAILED", f"cannot import {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            contribute = getattr(module, "contribute", None)
            if not callable(contribute):
                raise PluginError("LOAD_FAILED", "entrypoint must define contribute(api)")
            contribute(api)
        finally:
            sys.modules.pop(module_name, None)
    except PluginError as exc:
        plugin.diagnostics.append({"code": exc.code, "message": exc.message})
    except Exception as exc:  # defensive: a plugin can't crash the session
        plugin.diagnostics.append(
            {"code": "LOAD_FAILED", "message": f"{exc.__class__.__name__}: {exc}"}
        )
    plugin.tools = api.tools
    plugin.hooks = api.hooks
    return plugin


__all__ = ["LoadedPlugin", "PluginAPI", "load_plugin"]
