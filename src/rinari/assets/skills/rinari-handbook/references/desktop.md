# Rinari Agent (desktop) map

The desktop is a client of the Engine: everything it shows comes from Engine state, and every action goes through the Engine. Labels below are the Spanish UI; English mirrors them.

## Title bar
- ☰ toggles the sidebar (on narrow windows it opens it as a drawer). The empty bar drags the window.
- View switcher: **Normal** (one conversation), **Boards** (several sessions side by side), **Flujos** (how the work of a project or session advanced).
- Bell: board sessions that need attention. **Panel lateral** button: opens or closes the session's side panel at its last tab.

## Sidebar
- **Nueva conversación** (Ctrl+N), search over projects and sessions, **Proyectos** and **Conversaciones** lists. A session's menu (right click) renames, forks, archives or deletes it; archived sessions are listed apart and can be restored.

## Composer
- Attach files and images; permission profile; **PLAN / BUILD / REVIEW** mode.
- Context ring (left of the model): tokens the provider measured for the last request over the model window; hover shows `used / total`. Hidden when the provider does not report usage.
- Model picker: search, pick per session; **Actualizar modelos** re-reads providers and saves new models; **Administrar modelos** opens settings. Reasoning effort next to it.

## Side panel (per session)
- **Archivos**: files the conversation opened or changed; images and artifacts preview as images. A path in a message opens here.
- **Navegador**: the native browser the agent drives.
- **Workspace**: tabs **Cambios** (files changed per turn, review and undo), **Tareas**, **Verificación**, **Checkpoints**, **Artefactos** (images preview), **Contexto y uso** (context status, last compaction, compact now).

## Activity
- Each turn shows its steps: model calls, tools (with output), approvals, compactions with their checks, and file changes.

## Settings
General, Atajos, Apariencia, Proveedores (API keys, subscription login), Modelos, Visión e imágenes, **Contexto** (automatic compaction, threshold, summarizer), Agentes, Rinari / Soul, MCP, Plugins, Herramientas, Perfiles, Terminal, Avanzado, Acerca de (versions, updates).

## Install and updates
- Windows installer: **Instalar**, or for an installed copy **Actualizar** (newer version), **Reparar** (same version), **Modificar**, **Desinstalar**. Data in `~/.rinari` is kept.
- The app checks GitHub releases silently; with none published it simply stays up to date.
- Closing the window quits without asking; applying an update asks first.
