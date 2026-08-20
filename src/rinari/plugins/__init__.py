"""Plugin subsystem (phase 5).

Decision record (2026-08-17, see TODO.md "Registro de decisiones"):

- Manifest: `plugin.json` in the plugin directory. Declares name, version,
  description, entrypoint module and the **requested capabilities** (which
  contribution types the plugin may register). Per AGENTS.md 18 a plugin's
  requested capabilities must be explicit; registering an undeclared
  contribution type is a load error, not a silent grant.
- Sources: `user` plugins live in `~/.rinari/plugins/<name>`; `project`
  plugins live in `<root>/.rinari/plugins/<name>` and **require project
  trust** to load (AGENTS.md: untrusted project extensions do not execute;
  AGENTS.md 17 for the same rule on skills).
- Entry contract: the entrypoint module exposes
  `contribute(api: PluginAPI) -> None`. First-pass contribution types:
  `tools` and `hooks` (other manifest types are reserved; the API refuses
  them until their runtimes land).
- Tool namespacing: plugin tools must be named `<plugin>.<tool>`; plugins
  cannot inject into core namespaces.
- Loading never crashes the session: every load failure becomes a
  diagnostic (code + message), surfaced by `plugins doctor` / `permissions`.
"""

from rinari.plugins.loader import LoadedPlugin, PluginAPI
from rinari.plugins.manifest import (
    CONTRIBUTION_TYPES,
    PluginError,
    PluginManifest,
    load_manifest,
)
from rinari.plugins.service import PluginService

__all__ = [
    "CONTRIBUTION_TYPES",
    "LoadedPlugin",
    "PluginAPI",
    "PluginError",
    "PluginManifest",
    "PluginService",
    "load_manifest",
]
