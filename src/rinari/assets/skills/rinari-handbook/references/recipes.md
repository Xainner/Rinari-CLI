# Diagnostic recipes (three calls or fewer)

Each recipe names the calls in order. Stop when the data answers the question; report what you saw, quoting ids.

## "It ended the turn without answering"
1. `rinari.session` (the id, or `current`): find the turn with outcome `empty` or `tools_only`.
2. `rinari.turn` on it. If `anomalies` says output tokens were billed with no text and no tool call, the provider answered but the response was not parsed: name the provider and model, and say it is an Engine adapter problem (not the user's prompt). If a tool failed or an approval never resolved, say which.

## "The provider shows 0 models" / a model is missing
1. `rinari.status` with `provider`: auth method, whether a credential exists, connection, model count.
2. No credential: the user must reconnect in Settings > Proveedores. Credential present but no models: the catalog answered empty; suggest **Actualizar modelos** and, if it persists, report the provider type and endpoint.

## Subscription login "could not be completed"
1. `rinari.status` with `provider`: a missing credential after a finished browser login points to the save step.
2. The login detail names the failing step (authorization, token exchange, saving the credential); quote it. The browser page only says whether it connected.

## "File is outside this turn's workspace"
1. `rinari.turn` for the turn whose link failed: `changes` shows whether the session changed that file.
2. A file the session created or edited opens from any of its turns; a file it later deleted, or one it never touched outside its workspace, stays closed by design.

## Context, compaction and "it forgot"
1. `rinari.session`: `compaction_revision` and each turn's `input_tokens`.
2. `rinari.turn` on the turn that compacted: `compactions` (status, tokens before and after) and anomalies such as a failed compaction.
3. Windows and thresholds: `rinari.status` (`context` section); a manual window set from the CLI overrides what providers report.

## "Which session was the one where…"
1. `rinari.sessions` with `query` (title or message text), `model` or `since`.
2. `rinari.session` on the match.
