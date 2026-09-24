# Rinari Engine: how it works

## Shape
- **Engine** (Python package `rinari`): sessions, the agent loop, tools, policy, providers, context, memory, skills, souls. One Engine serves both clients.
- **CLI** (`rinari …`): terminal client of the same Engine and state.
- **Rinari Agent** (desktop, Electron): runs `rinari engine --stdio` as a sidecar and talks to it with a versioned NDJSON protocol. The desktop owns windows and presentation only; it never re-implements Engine logic.
- A session started in one client continues in the other (`rinari resume <id>`, or open it in the desktop).

## Home (`~/.rinari`, or `RINARI_HOME`)
- `state.db`: SQLite with sessions, messages, events, providers, models, tasks, artifacts metadata and changesets.
- `config.toml`: configuration (`rinari config`). `context-policy.json`: summarizer model and manual context windows.
- `credentials/`: file-backed secrets (fallback). Secrets normally live in the OS store (`keyring://`); records hold references, never values.
- `artifacts/<session>/<namespace>/<name>`: large outputs and media, addressed as `artifact://<session>/<namespace>/<name>`.
- `workspaces/chat_…`: the working folder of a CHAT session. `souls/`, `skills/`: user souls and skills. `active_soul`: the global soul.
- Model tools cannot read this folder; use `rinari.*`.

## Sessions and turns
- Kinds: **CHAT** (general; workspace under `workspaces/`) and **PROJECT** (bound to a repository root). A CHAT can be promoted or moved to a project.
- Modes: **plan** and **review** run read-only; **build** may edit within the permission profile (`read-only`, `workspace`, `full-access`).
- A turn = one user message and everything until the answer: model calls, tool calls, approvals, compactions. Its outcome is one of answered, tools_only, empty, failed, cancelled or interrupted.
- Events: the desktop layer (`turn.*`, `model.*`, `tool.*`, `usage.updated`, `approval.*`, `governor.*`) carries a turn id; the harness layer (`AgentTurnStarted`, `ModelInvoked`, `ToolRequested`, `ToolCompleted`, `AgentTurnCompleted`) is shared with the CLI. `rinari.turn` merges both.
- File changes per turn are recorded as a changeset (undoable). A file the session created or edited stays openable from any of its turns.

## Providers, models, credentials
- A provider = endpoint + auth (`api-key`, `oauth` subscription, `none`). A model = a saved alias of a provider model id.
- `model.refresh` from the desktop ("Actualizar modelos") re-reads every provider: availability, capabilities, context windows, and saves new models under their provider id.
- Subscription logins (ChatGPT, Copilot) are OAuth; long tokens are stored in parts in the OS store. The ChatGPT catalog lists only models for a recent `client_version`, and its stream delivers output items in `response.output_item.done`.

## Context and compaction
- Effective window per model, by precedence: manual (set from the CLI) > override on the model > what the provider reports > bundled catalog > 128k fallback.
- Compaction runs automatically at the threshold (percent of usable input, default 80-90%) and aims at about 60% of it, or at 75% of the threshold if that is lower. It keeps the goal, constraints, decisions and open work, and checks its summary against recorded tasks and validations.

## Tools
- Core tools are always visible; on-demand ones (browser, MCP, OpenAPI, plugins, `rinari.*`) appear after `capability.search` with `load=true`, `capability.activate`, or activating a skill that requires them.
- Every call goes through policy: reading harness state is always allowed; writes, shell and network follow the permission profile and may ask for approval.
- Large results spill to artifacts; read them with `artifact.read`.

## Skills and souls
- Skills: packaged, user (`~/.rinari/skills`) and project (`.rinari/skills`, trusted projects only). The catalog line of each is always in context; activating one injects its SKILL.md every turn and exposes its required tools; its `references/` are read with `skills.read` only when needed.
- Souls set identity and voice only; they never change policy, permissions or truth.
