"""`rinari update`: the desktop app and this CLI, from the published release.

A release of Xainner/Rinari-Agent carries the desktop installer and
`rinari-release.json`, which names the Engine commit the app ships. The app
updates from its installer; a CLI installed on its own is reinstalled at that
same commit so both stay compatible. The CLI bundled inside the app is the
app's Engine and moves with it.
"""
