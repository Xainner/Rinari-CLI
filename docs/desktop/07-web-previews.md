# Web previews

`web_preview_v1` adds `workspace.preview.start`, `workspace.preview.status`, and
`workspace.preview.stop`. Start accepts `session_id`, `path`, optional `turn_id`,
`run_dev`, and `dev_url`. Status/stop use `session_id` and `preview_id`.

Static HTML is served from its containing directory on an ephemeral loopback
port and random `.localhost` hostname. The server checks Host, canonical paths,
sensitive files, allowed web extensions and a 16 MiB per-asset limit. It has no
directory listing, writes, CORS grants, or desktop RPC endpoints. Root-relative
assets work. File provenance comes from the same resolver as workspace reads.

The desktop embeds an iframe on a different origin, with scripts/forms allowed
but without top navigation, popups, device permissions, or Tauri capabilities.
The desktop polls revisions for loaded resources and updates the frame and source.
Standalone browser pages use an injected reload script; the original HTML is
never modified. Developer-provided CSP can restrict this injected script.

Vite projects offer an explicit start action through the existing ProcessRegistry
backend, using their npm dev/start script with a loopback host and strict random
port. Read-only modes cannot launch it. Other frameworks can connect an existing
HTTP localhost URL. Attached servers are never terminated by Rinari.

Closing the viewer, switching its active file/session, or closing/deleting the
session releases its preview. Owned development processes are terminated as a
tree. Engine shutdown stops all previews; inactive previews expire after ten
minutes without a heartbeat. Moving a session with a live owned dev server is
blocked. Static previews keep their original directory after a session move.
