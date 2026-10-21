# Rinari CLI — Commands Specification

> **Version:** 2.1  
> **Purpose:** Canonical command surface for the Rinari harness.
>
> This document defines the commands, semantics, persistence rules, selection model, safety expectations, automation behavior, and implementation boundaries required for a production-grade Rinari CLI.

---

# 0. Command Design Principles

The CLI must follow these invariants everywhere:

1. **Selection is not deletion.**
2. **Configuration is not activation.**
3. **Switching provider, model, profile, mode, or session never destroys previously saved configuration.**
4. **Destructive actions require explicit verbs:** `remove`, `delete`, `clear`, `reset`, `revoke`, or `forget`.
5. **Plural nouns manage registries; singular nouns operate on the current selection or one item.**
6. **Read-only commands are safe by default.**
7. **Mutations report exactly what changed.**
8. **Important commands support `--json` for automation.**
9. **TTY and non-interactive behavior are both first-class.**
10. **Business logic is independent from TTY prompting so the same services can later power desktop/web.**

Canonical command pattern:

```text
rinari <noun> <verb>
```

Examples:

```bash
rinari providers list
rinari provider use openai-personal
rinari models list
rinari model use gpt-main
rinari skills activate fix-ci
rinari session resume ses_123
```

---

# 1. Semantic Vocabulary

Use these verbs consistently.

```text
add / create / install
  persist something new

use
  select an existing item as active/default

activate
  load something for the current task/session

enable
  allow a persistent extension/integration to participate

login / connect
  establish authentication or connection

logout / disconnect
  end authentication/connection but preserve configuration

disable / deactivate
  stop use while preserving configuration

remove
  remove one registry item

delete
  delete durable entity/history

clear
  remove accumulated contents

reset
  restore configuration/default state

forget
  remove a memory record

revoke
  remove an authorization/grant/credential permission
```

Never overload `use`, `login`, `logout`, `enable`, `disable`, `activate`, or `deactivate` with deletion.

---

# 2. Top-Level Command Surface

```text
rinari
├── setup
├── help
├── version
├── doctor
├── status
├── init
│
├── chat
├── ask
├── plan
├── agent
├── review
├── run
├── resume
├── stop
├── verify
│
├── provider
├── providers
├── model
├── models
│
├── config
├── profiles
├── bundles
├── project
├── trust
│
├── session
├── tasks
├── context
├── memory
├── artifacts
├── checkpoint
├── undo
│
├── permissions
├── approvals
├── sandbox
├── network
├── secrets
│
├── tools
├── skills
├── agents
├── plugins
├── mcp
├── api
├── hooks
│
├── trace
├── logs
├── eval
├── metrics
│
├── cache
├── index
│
├── export
├── import
├── update
├── completion
└── dev
```

Not every command needs to ship in the first release. The command namespace should still be reserved coherently from the beginning.

---


## Production integration requirement

The public CLI is expected to control the complete harness, including:

```text
browser
plugins
MCP
OpenAPI integrations
tools
skills
subagents / multi-agent
memory
context
artifacts
sandbox
permissions
approvals
verification
tracing
evals
```

These namespaces are not speculative placeholders in the target design. Their commands operate on live runtime subsystems that share the same state, policy, capability, and observability architecture.

---

# 3. Default Invocation

```bash
rinari
```

Starts the context-aware interactive experience:

```text
project detected
  → PROJECT session

no project detected
  → CHAT session
```

Explicit chat:

```bash
rinari chat
```

always starts/continues a **CHAT** session and does not automatically bind to a repository merely because the current directory is inside one.

Conceptual plain `rinari` startup:

```text
resolve cwd
  ↓
detect project root
  ├── found → PROJECT context
  └── none  → CHAT context
  ↓
resolve profile
  ↓
resolve provider
  ↓
resolve provider-specific model
  ↓
restore/create compatible session
  ↓
enter agent loop
```

Direct task:

```bash
rinari "fix the failing auth tests"
```

Equivalent to:

```bash
rinari run "fix the failing auth tests"
```

---

# 4. Global Flags

Common flags:

```text
-h, --help
-V, --version

--json
-q, --quiet
-v, --verbose
--debug

--config <path>
--profile <name>
--provider <alias>
--model <alias-or-provider-model-id>

--cwd <path>
--project <path-or-id>

--session <id>
--new-session

--mode <ask|plan|agent|review|full-access>

--sandbox <profile>
--approval-policy <name>

--offline
--no-network

--non-interactive
-y, --yes
--dry-run

--timeout <duration>
--max-cost <amount>
--max-turns <n>

--no-color
--color <auto|always|never>
--no-progress
```

Important rule:

```text
--provider
--model
--profile
```

are temporary invocation/session overrides unless an explicit configuration command persists them.

Permanent provider selection:

```bash
rinari provider use anthropic-work
```

Permanent model selection for that provider:

```bash
rinari model use opus
```

---

# 5. Persistence Model

Rinari must distinguish four states:

```text
SAVED
  configured and retained in registry

ACTIVE DEFAULT
  selected for normal future sessions

SESSION OVERRIDE
  selected for one interactive session

COMMAND OVERRIDE
  selected for one invocation
```

Example:

```text
Saved providers:
  openai-personal
  anthropic-work
  google-personal
  local-ollama

Active default:
  openai-personal

Command:
  rinari --provider anthropic-work "review this PR"

Result:
  this invocation uses anthropic-work
  active default remains openai-personal
  no provider configuration is removed
```

The exact same principle applies to models.

---

# 6. `setup`

Initial onboarding.

```bash
rinari setup
```

Recommended interactive flow:

```text
1. User identity / display name
2. Language
3. First provider
4. Authentication method
5. First model
6. Default capability profile
7. Network behavior
8. Optional telemetry
9. Shell completion
10. Installation verification
```

Current scope (v0.1): the wizard implements steps 3-5 (provider type/alias,
endpoint for custom, authentication via api key / env var / none) plus the
first model, all fully usable in combination with the one-shot flags. Identity,
language, profile, network, telemetry and completion steps are later onboarding
phases; the `--minimal` and `--reset` variants are not implemented yet.

Useful variants:

```bash
rinari setup --minimal
rinari setup --provider openai
rinari setup --non-interactive
rinari setup --reset
```

Running setup again must not erase configuration.

If existing state is detected:

```text
Existing Rinari configuration found.

  Edit defaults
  Add another provider
  Change active model
  Change permission profile
  Re-run diagnostics
  Cancel
```

Only `--reset` may intentionally rebuild setup state, and it must preview what will be affected.

---

# 7. `help`

```bash
rinari help
rinari help providers
rinari help providers add
rinari providers --help
```

Help should show:

```text
usage
description
arguments
flags
examples
side effects
configuration affected
risk when applicable
related commands
```

---

# 8. `version`

```bash
rinari version
rinari --version
rinari version --json
```

Recommended fields:

```text
Rinari CLI
Harness version
Config schema
Tool protocol
Plugin API
Skill API
Session export schema
```

---

# 9. `doctor`

```bash
rinari doctor
```

Checks:

```text
config validity
config migrations
provider credentials
active provider
active model
model accessibility
secret store
shell
git
sandbox support
filesystem permissions
session database
artifact store
plugins
MCP servers
project trust
repository index
secret redaction
language tooling
```

Variants:

```bash
rinari doctor --fix
rinari doctor --provider anthropic-work
rinari doctor --project .
rinari doctor --json
rinari doctor --verbose
```

`--fix` may repair safe local issues, but must not silently rotate credentials, delete state, trust projects, or widen permissions.

---

# 10. `status`

Fast operational overview and canonical visual runtime snapshot.

```bash
rinari status
```

Show, when known:

```text
Rinari version
CHAT / PROJECT
mode
provider alias + provider type
model alias + provider model ID
reasoning effort/mode
context used / context window / percentage
session input/output tokens
cached input tokens
reasoning tokens when provider reports them
cost with exact/calculated/estimated provenance
session runtime
model calls + tool calls
project root/cwd
Git branch + dirty state
sandbox/profile + network policy
active tools + skills + subagents
pending approvals
MCP/plugin/browser state when active
```

Example:

```text
Rinari v0.8.0                                      PROJECT / agent
────────────────────────────────────────────────────────────────────────────
Model       openai-personal :: gpt-main
ID          <provider-model-id>
Reasoning   high                     Tools        24 loaded / 143 available
Context     31.2k / 128k  [24%]      Skills       debug, final-verification
Session     in 42.8k  out 8.7k       Agents       2 running / 4 max
Cache       18.3k input              Approvals    none pending
Reasoning   6.1k tokens*             Cost         ~$0.184*

Project     ~/code/app
Git         feature/oauth *          Sandbox      workspace
Network     ask                      Session      ses_01J...
Runtime     00:02:17                 Tool calls   38
────────────────────────────────────────────────────────────────────────────
* only when supported; `~` means estimated
```

Unknown/provider-unavailable values render as `—`; never fabricate them.

```bash
rinari status --compact
rinari status --verbose
rinari status --json
```

`--json` returns exact integers and provenance for measured/provider-reported/calculated/estimated fields.

---

# 11. `init`

Initialize project integration.

```bash
rinari init
```

May create:

```text
RINARI.md
.rinari/config.toml
.rinari/skills/
```

Options:

```bash
rinari init --minimal
rinari init --detect
rinari init --template typescript
rinari init --force
```

Never overwrite existing project instructions without explicit confirmation or `--force`.

---


# 12. `chat`

Explicit conversational mode.

```bash
rinari chat
rinari chat "help me design this architecture"
```

Semantics:

```text
session kind = CHAT
project binding = none by default
```

This remains true even when invoked from inside a repository:

```bash
cd ~/code/app
rinari chat
```

The command is useful when the user wants Rinari as a general assistant rather than immediately entering repository-agent mode.

A CHAT session can still use permitted global tools, connectors, web, browser, artifacts, memory, MCP/plugins, and subagents.

## CHAT → PROJECT promotion

CHAT is not a dead-end mode.

If the user explicitly asks Rinari to create/adopt a project in the current or selected folder, the same session may promote to PROJECT after initialization succeeds.

Examples:

```text
"initialize this folder as git"
"create a project here"
"scaffold a Node app here and initialize git"
"clone this repository and work on it"
"run rinari init here"
```

Required behavior:

```text
CHAT
  → bounded candidate workspace from explicit user intent
  → create/adopt project
  → re-run project detection
  → same session becomes PROJECT
  → preserve conversation/provider/model
  → load project state/instructions/tools/skills/policy
```

No CLI restart is required.

Merely discussing code or reading a directory does not trigger promotion.

Implementation (phase 2):

- The candidate workspace for a CHAT session is the directory the user
  explicitly opened: file writes inside it are allowed (bounded, and never
  `$HOME` itself); creating a project still goes through the normal tool
  policy (approval for shell where applicable).
- Promotion is detected by a strong project marker (`.git` or
  `.rinari/project.toml`) appearing at the session's own cwd when a turn
  completes. Walking up the directory tree is never a promotion trigger, so
  an explicit `rinari chat` inside a repository is not silently reverted.
- On promotion the session ID, conversation, provider, and model are
  preserved; the workspace sandbox and prompt context (project instructions,
  runtime policy summary) are recalculated in the same session, and
  `SessionPromotedToProject` + `SessionPromotedInProcess` events are traced.

`rinari chat` therefore means:

```text
start in CHAT
```

not:

```text
forbid this conversation from ever becoming a project
```

if the user later explicitly asks to create/adopt one.

---

# 13. Work Commands


## `ask`

Read-oriented repository interaction.

```bash
rinari ask "where is authentication initialized?"
```

Default:

```text
filesystem read
repository search
git read
safe diagnostics
no workspace mutation
no external mutation
```

---

## `plan`

Investigate and produce a plan without implementing by default.

```bash
rinari plan "migrate Express 4 to Express 5"
```

Expected output:

```text
relevant code
constraints
risks
ordered implementation steps
validation strategy
open decisions
```

---

## `agent`

Normal autonomous workspace mode.

```bash
rinari agent "fix the failing auth tests"
```

Default capabilities come from the selected profile, normally `workspace`.

---

## `review`

Read-only review by default.

```bash
rinari review
rinari review --base main
rinari review --pr 412
```

Optional explicit mutation:

```bash
rinari review --fix
```

---

## `run`

Generic task command, especially useful for automation.

```bash
rinari run "fix issue #123"
rinari run "review this branch" --json --non-interactive
```

---

## `resume`

Shortcut for session resume.

```bash
rinari resume
rinari resume ses_123
```

If no ID is given, resume the most recent compatible session for the current project.

---

## `stop`

```bash
rinari stop
```

Cancels current session execution and propagates cancellation to model stream, tool calls, shell processes, browser work, and subagents.

---

## `verify`

```bash
rinari verify
rinari verify changed
rinari verify task
rinari verify project
```

Runs the verification planner and records structured validation results.

---

## `engine`

Machine transport for desktop clients (Rinari Agent Engine Protocol v1).

```bash
rinari engine --stdio
```

- stdin receives NDJSON requests, stdout emits NDJSON responses/events.
- stdout is protocol-only: no banners, no ANSI formatting.
- stderr carries diagnostics and log lines only.
- The first stdout line is the `hello` handshake (`rinari-engine`, protocol version, engine version, capabilities).
- Every request carries an `id`; every response echoes it.
- Unknown methods, duplicate request ids, malformed input, and broken frames return stable error envelopes without breaking the stream.
- `engine.diagnostics` returns what a problem report needs, and never message or file contents, credentials, private endpoints, MCP commands or request parameters. It covers versions and platform, data sizes (session, message and event counts, the five largest sessions by id and bytes, database and artifact sizes), providers by product/type with the endpoint classified as `known`/`local`/`custom`, MCP and plugin state, and the latest 50 failed or slow (≥ 2 s) requests by method, code and duration. Each section degrades on its own. The desktop bundles it into "Exportar diagnóstico".
- No stdout line exceeds 16 MiB, the desktop's line limit. `session.history` and `session.timeline` pages also stop at 8 MiB: older rows are left for `has_more`/`next_before_turn_index`, and a single row that is still too large has its long strings shortened (data URLs omitted). The stored data stays intact. A larger response becomes `RESPONSE_TOO_LARGE` for that request only. A larger event has its strings shortened, or is dropped if that is not enough. Each case is reported on stderr.

Slice 1 methods: `engine.info`, `session.list`, `session.get`,
`session.create`, `session.open`, `runtime.snapshot.get`.
Slice 2 adds live turns and reads: `session.turn.start`,
`session.turn.cancel`, `approval.resolve`, `provider.list`, `model.list`,
with `turn.*`, `model.content.delta`, `tool.*`, and `approval.*` events.
Slice 3 adds provider/model management: `provider.create`, `provider.get`,
`provider.update`, `provider.remove`, `provider.test`, `provider.discover`,
`provider.use`, `model.get`, `model.add`, `model.alias`, `model.remove`,
`model.use`, `model.discover`, `model.refresh`, `model.test`.
Provider views are redacted by construction (`has_credential` only, never
secret material); new secrets are stored via the credential backend
(`env://`, `keyring://` when an OS store is functional, `file://`
fallback) and request params are never logged. A secret longer than one
OS entry allows (Windows: 1280 UTF-16 units, such as an OAuth bundle with a
JWT) is stored as parts under `<service>#part-<n>` behind a header with the
part count and digest; `secrets cleanup` treats each part like its entry.
`model.refresh` accepts `add_new` (default false): models the provider now
offers are saved under their provider ID (suffixed `-2`, `-3`… if the alias
is taken), providers without saved models are read too, and each provider
result lists them in `added`.
Slice 4a adds `session.history` (`ref`, `limit` 1..500 default 200):
persisted conversation rows (`seq`, `role`, `content`, `tool_calls`),
tail window with `total`/`has_more`. Protocol turns persist through the
same `run_turn` path as the terminal, so history covers desktop turns.
Slice 5a adds `session.mode.set` (`ref`, `mode` plan/build/review):
PLAN/REVIEW turns run under a READ_ONLY policy profile, BUILD under
WORKSPACE; legacy modes keep workspace behavior. Emits
`session.mode.changed`; the mode switch keeps session, tasks and context.
`workspace.file.resolve` (`session_id`, `path`, `turn_id`) authorizes a file
exactly like `workspace.file.read` (session root, turn provenance, private
paths) but never reads it as text: it returns `path`, `name`, `size`, `kind`
(`image`, `video`, `audio`, `pdf`, `text` or `binary`, from the first bytes),
`mime` and `preview_limit`. The desktop uses it to play media, open a file
with the system app or reveal it in its folder, at any size.
`artifact.resolve` (`uri`) does the same for an artifact: `path` of its stored
bytes, `name`, `size`, `kind` and `mime`, so an audio or video artifact plays
in the desktop instead of being read as text.
`session.open` also returns `branch_change` (`from`, `to`, `since`,
`reference`, `session_id`) when a `[git-branch]` warning was produced. The
reference is the branch last observed while Rinari worked in that git worktree
(`reference: last_work`, written at the start and end of every turn, from any
conversation), or the checkout the first time a worktree is seen
(`first_seen`; that first sight is recorded silently). Each change is reported
once. `sessions.git_branch` keeps the historical branch a conversation started on.
Attachment references carry `coverage` for PDFs (`total_pages`,
`prepared_pages`, `text_pages`, `ocr_pages`, `empty_pages`,
`unprocessed_pages`, `failed_pages`, and `pages[]` with each page's `method`
and `reason`) and accept `keep_image` with `ocr` on images: the OCR text and
the pixels both reach the model (capability `reading_coverage_v1`). Every
attachment, images included, is listed for the model with its artifact URI and
sha256, so tools can reuse the original.
`turn.stopped.details.stop` names a loop stop (`loop`: the detector kind,
`subject`: what repeated), so clients word it in their language.
A stream that ends without its terminal event fails with
`STREAM_INTERRUPTED` and `close` (`eof` or `done_marker`), `transport`,
`endpoint` (no query or credentials), `http_status`, `request_id`,
`bytes_received`, `elapsed_s`, `idle_s` and `partial_tool_calls`.
Every stored title emits `session.renamed` (`session_id`, `title`,
`previous_title`, `source`: `manual`, `generated` or `fallback`). The first message names the
session through the model; when no usable title comes back, the trimmed
message is a provisional `fallback` and later turns retry from that same
message (three failures at most, each a `SessionTitleFailed` event with only
its reason). A manual rename ends the retries.
The first text of a turn written by a different model than the last one that
wrote in the session is followed by `model.changed` (`after_model_call_id`,
`previous`, `next`, each with `model_id`, `alias`, `provider_model_id`,
`provider_alias` as named at that moment). A model that was selected but failed
before writing does not count. The event is persisted with the turn, so
`session.timeline` rebuilds it in place.
Slice 6a adds project workspace reads: `task.tree/get`,
`verification.latest/plan`, `checkpoint.list/show/restore`,
`project.changes` (porcelain files + branch/head/dirty, `available:false`
outside a repo), `project.diff` (unified, truncated, binary-safe).
Slice 7a adds agent routing: `agent.list` (definitions + assignments),
`agent.config.get/set` (model/fallback/enabled/effort per agent, `clear`
to reset; aliases must resolve and support tool calls; effort is low,
medium, high or null/inherit, anything else is `INVALID_PARAMS`),
`session.events` (persisted lifecycle incl. SubagentStart/Stop). Spawn
resolves assigned model → fallback → parent caller, and the stored
effort (when set) reaches the invocation through the existing
`reasoning_effort` call path.
Slice 8a adds Soul 3.0: `soul.list/get/create/update/remove/activate`,
`~/souls/<id>/{soul.toml,identity.md}` store, bundled `rinari-default`
(4.0 since 2026-10-09, see docs/soul.md), global activation; legacy
`~/soul.md` keeps working (active > legacy > bundled). Main-agent prompt uses
the active soul; subagents stay functional. `soul.settings.get` →
`{settings: {character_intensity, options, default}}` and `soul.settings.set
{character_intensity: "minimal" | "balanced" | "full"}` (same result;
anything else is `INVALID_PARAMS`; capability `soul_character_intensity_v1`)
scale how much of whichever Soul is in effect shows; default `balanced`.
Plan 0.4.0 #87: `soul.export {id, folder}` writes `<folder>/<id>/{soul.toml,identity.md}`
(`<id>-2`… never over an existing folder) and `soul.import {paths}` takes that
folder, or the `.toml` and its `.md` (a lone `.toml` finds `identity.md` or
`<name>.md` beside it), saved as a new custom soul with a free id. A soul's
text may be empty: it then speaks only with the character level.
Slice 9 adds the ecosystem surface: `mcp.list/get/create/remove/enable/disable/test`
(remote servers, `mcp.update` and `mcp.probe`: see `mcp_remote_v1` below)
(plain secrets rejected, test failures reported not raised), `plugin.list/get/enable/disable/diagnostics`
(doctor merged into list), `tool.list` (native registry), `policy.get`
(mode→profile mapping; souls/profiles never relax it). No engine-level
browser session is tracked in v1 (browser runs per-turn).
Slice 10 adds observability: `artifact.list/read` (URI-addressed, bounded,
truncated flag, no internal paths leak), `artifact.export` (`uri` +
existing `dest_dir` → real path; basename-contained, numeric suffix on
collision, never overwrites), `context.get` (compaction state +
counts from CompactState, no invented pressure %), `usage.get` (model calls
+ tokens + tool calls aggregated from persisted events; cost always null —
pricing unknown). No `pty.*` in v1 (see the post-v1 runtime entry below).
Slice 11 adds workflow: `session.queue.add/list/clear` (bounded FIFO,
auto-runs after the live turn with normal turn boundaries + approvals,
`session.queue.updated` events). Permissions v3 (`permissions_v3`): the
profiles are cut by what cannot be undone. read-only reads anything (files
and the internet) and never writes, runs or sends. workspace is free inside
the project or chat folder, on localhost/LAN and reading the internet; it asks
once to write or run outside, send data to an internet host (a request body or
a non-GET method, `ssh.inspect` or `ssh.run` to a public host), call an MCP tool, interact
with a web page or `git push`. A command given its own `cwd` (shell,
process, pty) is judged where it runs: its relative paths resolve from
there, and outside the project only plain inspection (`ls`, `cat`,
`git status`/`log`/`diff`…) runs without asking (`shell_cwd_outside`).
full-access asks for none of that. Every profile
asks for the hard list (force push, deleting outside the project; never granted
for good) and for system secrets (`~/.ssh`, GPG/cloud keys, OS and browser
credential stores; project `.env` files are ordinary work). Once a turn has read
external content (an internet page or API, an MCP result), sending data out
asks even in full-access (`rule_id = external_content_send`). Approvals offer
`allow_project`: "always" for this project, or for every loose chat, kept in
`policies/project_grants.json`, bound to the rule that asked (a host, a folder
or the rule itself) and listed/revoked with `permission.grants.list` /
`permission.grants.revoke`. `network.mode` defaults to `auto` (the profile
decides); `ask` keeps the old per-host prompt, `allow`/`off` as before. An allow rule
or `network.mode = allow` relaxes prompts only: read-only still never sends,
and after external content a send to an internet host still asks.
A turn started with a command keeps it on the owner message:
`origin = {kind: "user", command, command_kind: "command" | "skill"}`, so a
client shows a chip instead of the raw `/name`. `capability.search` also
returns matching skills (`skills[]`) and tells the model to activate the first
one; skill validation checks required tools against every built-in tool
(`rinari.*`, `skills.*`…), not only the native ones, and skips MCP/plugin tools.
Steering (`turn_steering_v1`):
`session.turn.steer {session_id, message}` puts a message into the running
turn instead of after it. Nothing is interrupted: the loop adds it to the
history after the current step (a tool round, or an answer, which then stops
being final) and the model continues with it. The model reads a short note
that the owner sent it mid-task; the chat shows only the owner's text
(`origin.steer_id`). Event `steer.applied {steer_id, content}` marks where it
was read. With no turn running it is an ordinary message (`delivery:
"queued"`, started at once); one that arrives while the turn is finishing
becomes the next turn; one left unread by a turn that was stopped or failed
comes back in `steer.returned {messages, reason}` instead of starting work
the owner just stopped. Desktop only for now: the REPL reads input between
turns. `profile_bundle.list/get/create/apply/remove`
(soul + mode + per-agent models applied through the existing setters with
an applied-report; no policy invention), and `rinari desktop [path] [--session id]`
handoff to Rinari Agent (`rinari code` remains a compatibility alias).
Binary resolution: explicit `--binary`, then `RINARI_AGENT_BIN`, then legacy
`RINARI_CODE_BIN`, then `rinari-agent` on PATH, then legacy `rinari-code` on PATH.
An invalid explicit path/environment override fails instead of silently launching
another executable. Explicit --project/--session args preserve single-instance routing.
`rinari agent` continues to execute autonomous tasks; it does not open the desktop.
Post-v1 lifecycle: `session.close` (`ref` → state `closed`: hidden from
default `session.list` unless `include_closed`, restorable via resume,
rejects new turns with `SESSION_CLOSED`; rejected with `TURN_RUNNING`
while a turn runs) and `session.delete` (`ref`, `cascade` default false;
running turn → `TURN_RUNNING`; always drains the turn queue and reports
`queue_dropped` + `checkpoints_removed/kept` + `artifacts_removed/kept`;
cascade also removes the session's checkpoints and session-retention
artifacts; tasks are project-scoped and never deleted with a session).
`session.branch` (`ref`, `title?`, `checkpoint_id?` as history upper
bound → independent copy of conversation + compact state + checkpoints
with `branched_from` ancestry; task graphs not copied).
Post-v1 projects: `project.list_recent` (`limit` 1..100 default 20,
ordered by shared session activity, never a second store;
`last_opened_at` falls back to record recency; `active_session_id` binds
the latest non-closed session; `git_head` `{branch, detached, sha_short,
operation}` read from `.git/HEAD` without running git, also in
`project.list`/`project.get`), `project.open` (`path` → upsert +
recommended session, created or reused-and-touched; `$HOME` rejected;
plain dirs promote to PROJECT) and `project.status` (git truth +
`active_session_id` dashboard binding; branch/dirty live here, not in
the recents list). `project.intelligence` (`path` → read-only repo
understanding: languages/frameworks/commands, index status, instruction
scopes honoring the trust gate; uncomputed fields omitted, never
fabricated).
Post-v1 soul: `soul.get_effective` (`ref` → `soul_id` + `source`
session/global/legacy/default), `session.soul.set` (`ref` + `id`; unknown
ids rejected NOT_FOUND before writing) and `session.soul.clear`
(`ref` → back to the global chain); the pin reaches prompt composition
and survives restarts; a pin to a removed Soul fails loudly.
Post-v1 agents: `model.capabilities` (`provider` + `provider_model_id` →
normalized map tools/streaming/structured_output/reasoning/vision/
max_context_window with `supports_tools` + `unknown`; vision is always
unknown until an adapter determines it, never an invented false).
Post-v1 runtime: `pty.start` (`command`, `cwd?`, `env?`, `cols?`,
`rows?`, `session_id?` → user-initiated terminal over the shared PTY
backend) + `pty.write/resize/read/list/terminate` with `pty.output` /
single `pty.exit` events; cwd never the home root; dead-handle terminate is
no-op success; restart reports no handles.
Desktop terminal (`desktop_terminal_v1`): Windows spawns through ConPTY
(pywinpty, a win32-only dependency), so `PTY_UNSUPPORTED` now means only a
missing backend. `command` is optional: without it the default of
`pty.shells` opens (PowerShell 7, then Windows PowerShell, cmd and Git Bash
when found; the user's login shell on POSIX). `pty.write {raw: true}` sends
keystrokes as typed (no newline appended). Output is kept in a 256 KB tail
addressed by byte offset: `pty.output` carries the `offset` where its chunk
ends and `pty.read` returns the tail plus its end `offset`, so a terminal
that reattaches repaints from `pty.read` and skips events it already has.
A chat without a project whose cwd is the home root opens in `~/Documents`
(an explicit home-root `cwd` is still refused). The model-facing `pty.*`
tools are unchanged and stay POSIX-only.
Scheduled tasks (`scheduled_tasks_v1`): `schedule.list/get/create/update/
delete/runs/run_now/grant`, events `schedule.changed`,
`schedule.run.started`, `schedule.run.needs_you` and
`schedule.run.completed`. A task is a reminder or a prompt for an agent turn
(project or chat, mode, model, skills) on a local-time schedule (`once`,
`interval` of 5 min or more, `daily`, `weekly`). The serving Engine checks
every 20 s; a run due while it was down still runs within 12 h and is
recorded `skipped` after that, as is one whose previous run is still active.
An agent run is a turn in its own session ("⏰ name · date") with
`origin.kind = "schedule"`. The task's `grants` ({capability, target?}) are
seeded as session grants of that session: creating the task approves them.
Anything else asks as usual: the run turns `needs_you`, and if the approval
expires the run ends `blocked` with what it needed as its reason.
`schedule.grant` adds a capability to a task by task id or by the run's
session ("allow for this task"). The lazy `schedule.propose` tool creates
the task at once when the owner asked in their own turn (`origin_kind` user)
and only proposes it otherwise (another pane, another task); either way it
carries no grants, so a run asks for what it needs. The desktop receives it
whole as a `schedule.proposed` event (`created`, and `task` when created).
Remote MCP (`mcp_remote_v1`): servers are `transport: "stdio"` (a
`command` array) or `"http"` (a `url`, http/https, no userinfo) speaking the
Streamable HTTP transport of MCP 2025-03-26: one POST per JSON-RPC message
with `Accept: application/json, text/event-stream`, a JSON body or an SSE
stream as the answer, the `Mcp-Session-Id` from `initialize` resent on every
request, `MCP-Protocol-Version` once negotiated and `DELETE` of the session on
close; TLS is always verified. The legacy HTTP+SSE transport is not supported
(`MCP_TRANSPORT_UNSUPPORTED`, hint `legacy_sse_unsupported`).
`mcp.create {name, transport?, command?, url?, env?, env_refs?, auth?,
headers?, timeout_s?}` and `mcp.update {name, …same fields}` share the shape:
`auth = {kind: "none" | "bearer" | "headers", token?}`, `headers = {Name:
"plain" | {value, secret?} | null}`, `env = {NAME: secret | null}` (stdio),
`timeout_s` 1..300. A secret is a literal string, `{value}`, or `"env://NAME"`
/ `{env: NAME}`; literals go to the credential store under
`mcp/<server id>/<slot>` and the row keeps only the reference; `keyring://` or
`file://` references from a client are refused (`INVALID_PARAMS`), so a server
can never be pointed at another stored secret. Header names that look
sensitive (auth, token, key, secret, password, cookie, session) default to
`secret: true`. `mcp.update` merges `env`/`headers` per key (`null` removes
one and retires its stored secret), replaces `auth` (`{kind: "bearer"}`
without `token` keeps the stored token; `kind: "none"` clears it); switching
transport drops the other transport's fields. Views (`mcp.list/get/create/
update/enable/disable`) add `url`, `argv`, `timeout_s`, `auth {kind,
token?}`, `headers [{name, secret, configured, value? | source, env_var?}]`,
`env [{name, configured, source, env_var?}]` and `warnings`
(`plain_http_remote`): flags and env-var names, never a value.
`mcp.test {name}` connects with a fresh client (the cached connection used by
turns is untouched; a disabled server can be tested) and `mcp.probe {name?,
…fields}` tests a configuration without saving anything; with `name` the
fields patch that saved server, so an edit form tests a new URL without
re-typing a stored token. Both return `{test}` and never raise for connection
problems: `{ok, server, transport, latency_ms, server_info {name, version},
protocol_version, tools, names, resources, prompts}` (`resources`/`prompts`
are null when the server does not advertise them) or `{ok: false, code,
message, http_status, hint, retryable, latency_ms}`. Codes:
`MCP_AUTH_REJECTED` (401/403), `MCP_NOT_FOUND` (404, or an unregistered
name), `MCP_UNREACHABLE`, `MCP_TLS`, `MCP_TIMEOUT`, `MCP_PROTOCOL`,
`MCP_HTTP_ERROR`, `MCP_TRANSPORT_UNSUPPORTED`, `MCP_SESSION_EXPIRED`,
`MCP_SPAWN_FAILED`, `MCP_SECRET_MISSING`, `MCP_NOT_CONNECTED`,
`MCP_DEPENDENCY` (a stdio server that exited). Hint keys: `check_credentials`,
`oauth_required` (a 401 and no OAuth sign-in: sign in, see `mcp_oauth_v1`),
`reauth_required` (the OAuth sign-in expired and could not be renewed),
`protocol_version_unsupported`, `check_url_or_network`,
`check_tls_certificate`, `server_slow_or_unresponsive`, `check_endpoint_path`,
`server_not_registered`, `not_an_mcp_endpoint`, `initialize_rejected`,
`server_error`, `request_rejected`, `legacy_sse_unsupported`,
`session_expired`, `check_command_installed`, `server_exited`,
`secret_missing`, `server_disabled`, `project_not_trusted`. Messages carry no
header values, tokens or query strings.
Background connections (`mcp_background_connect_v1`, connectors plan C2):
the Engine connects every enabled global server when it starts, in parallel,
each on its own thread; a project server connects when a session of a
trusted project builds its runtime. No turn waits for any server: a server
that arrives later joins the running turn's registry (`capability.search`,
`tool.call`) and the turn note's index of what is connected, never the
model's tool list (F42). The connection, its client and its `tools/list`
live in the Engine process and are shared by every session; `tools/list` is
read again only when the server sends `notifications/tools/list_changed`
(over Streamable HTTP, only when it arrives on a POST's event stream: the
standalone GET stream is not opened). Views add `status` (`idle` before any
attempt, `connecting`, `connected`, `reauth_required`, `error`, `disabled`)
and `connection {code, hint, message, tools, resources, attempts,
retry_in_s, changed_at}`; `mcp.status.changed {name, status, connection}`
is emitted on every change, and `engine.diagnostics` carries the same per
server. A failed server is retried with a growing wait (2 s, 4 s, 8 s… up to
5 min; six timer retries in a serving Engine), by a call to one of its tools
once the wait is over, or at once with `mcp.reconnect {name}` (returns
`{server}`, usually `connecting`; the outcome arrives as the event). A call
inside the wait fails at once with the stored reason instead of waiting for
a timeout. `reauth_required` (renewing a sign-in failed) and failures the
owner must fix (`check_credentials`, `secret_missing`, …) are never retried
on a timer. Connected servers that offer resources add two on-demand reads
for the model, `mcp.resources.list {server?}` and `mcp.resources.read
{server, uri}` (`mcp.read`); binary contents are described, not inlined.

OAuth for remote MCP (`mcp_oauth_v1`, plan de conectores C1): `initialize`
asks for MCP `2025-06-18` and follows the server down to `2025-03-26` or
`2024-11-05` (any other answer: `MCP_PROTOCOL`, hint
`protocol_version_unsupported`). `auth = {kind: "oauth", client_id?,
client_secret?, scopes?, redirect_port?, client_metadata_url?,
authorize_params?}` on `mcp.create`/`mcp.update` (merged over the stored one;
`null` clears a key): `client_id` is the owner's own OAuth client (public, kept
in the row), `client_secret` a secret like any other (credential store, slot
`oauth-client-secret`), `scopes` a list or a space-separated string,
`redirect_port` 1024..65535 for clients that need an exact redirect,
`client_metadata_url` an https URL with a path (a client ID metadata
document; the process default is `RINARI_MCP_CLIENT_METADATA_URL`), and
`authorize_params` extra authorization-request parameters (never
`response_type`, `client_id`, `redirect_uri`, `state`, `code_challenge*`,
`resource` or `scope`). Views add `client_id`, `client_secret` (state),
`scopes`, `redirect_port`, `client_metadata_url` and `authorize_params`.
`mcp.auth.start {name, open_browser?: true, timeout_s?: 300}` (oauth servers
only, otherwise `INVALID_PARAMS` with `details.hint` `oauth_not_configured`)
discovers the authorization server — protected resource metadata (RFC 9728)
at `/.well-known/oauth-protected-resource<path>` even without a 401, else the
`resource_metadata` of an anonymous `initialize`'s 401, else the authorization
server on the MCP origin (RFC 8414 / OpenID discovery) — picks the client
(the configured `client_id`; else dynamic registration, RFC 7591, kept per
server and reused on its port; else the client metadata document when the
server accepts them; else `MCP_AUTH_FAILED` with `details.hint`
`oauth_client_required`), opens a listener on
`http://127.0.0.1:<free or redirect_port>/callback` for that sign-in only, and
returns at once `{name, flow_id, authorize_url, redirect_uri, browser_opened,
expires_at, client, issuer, resource, resource_metadata, registration,
client_metadata_document, revocation}`; the URL carries PKCE S256, `state` and
`resource` (RFC 8707), plus `access_type=offline` (and `prompt=consent` while
no refresh token is kept) for Google. Errors before the browser step are
`MCP_AUTH_FAILED` with `details {code, message, hint}` (`OAUTH_DISCOVERY_FAILED`
`oauth_metadata_missing` / `oauth_metadata_invalid` / `oauth_pkce_unsupported`,
`OAUTH_CLIENT_REQUIRED`, `OAUTH_REGISTRATION_FAILED`
`oauth_registration_rejected`, `OAUTH_PORT_IN_USE` `redirect_port_busy`,
`OAUTH_UNREACHABLE`, `OAUTH_INSECURE_URL` `https_required`: OAuth needs an
https URL, http only on a loopback host, and `mcp.add`/`mcp.update` refuse
it too; `OAUTH_CANCELLED` `oauth_cancelled` when `mcp.auth.cancel` arrives
while it still discovers or registers: the browser never opens). A callback
with another `state` is refused (400) and the sign-in keeps waiting. Events: `mcp.auth.progress {name, flow_id, stage}`
(`discovering`, `registering`, `waiting_for_browser`, `exchanging_code`) and
`mcp.auth.finished {name, flow_id, outcome, finished_at, error?}` with
`outcome` `signed_in`, `failed` (`OAUTH_DENIED`, `OAUTH_TOKEN_REJECTED`…),
`cancelled` or `timed_out`. `mcp.auth.status {name}` → `{name, kind,
signed_in, reauth_required, expires_at, has_refresh_token, scopes, issuer,
client, pending {flow_id, authorize_url, started_at, expires_at} | null, last
{flow_id, outcome, finished_at, error?} | null}`; `mcp.auth.cancel {name}` →
`{name, cancelled}`; `mcp.auth.signout {name}` revokes the tokens when the
server has a revocation endpoint (RFC 7009), deletes them and drops the cached
connection → `{name, revoked, removed, cancelled}`. Tokens (access, refresh,
expiry, scopes) live only in the credential store under `mcp/<server
id>/oauth` (a registered client under `mcp/<server id>/oauth-client`); the
transport refreshes a token about to expire and, after a 401, refreshes and
retries once; a refresh answered 408, 429 or 5xx keeps the refresh token and
is retried later. When that is impossible the error is `MCP_AUTH_REJECTED` with
hint `reauth_required` (`oauth_required` if never signed in). `mcp.remove`
revokes and deletes them too. `mcp.test`/`mcp.probe` add `oauth` for http
servers with auth `none` or `oauth`: `{available, client: configured |
registration | metadata_document | required, issuer, resource,
resource_metadata, registration, client_metadata_document, revocation}` or
null. No token, code, verifier or client secret is ever in a view, an event,
a log line or an error message.

Permissions per connector and per tool (`mcp_tool_permissions_v1`, plan de
conectores C5), on top of the three modes. The server's annotations count:
`readOnlyHint: true` is a read (`mcp.read`), `destructiveHint: true` on a
tool that writes asks in project mode (risk high, with «always»), and a tool
that writes without `openWorldHint: false` may send data outside. They are
hints, not guarantees: only an explicit `true`/`false` counts, and the owner
can correct them per tool. Allow / Ask / Block per tool wins over the
server's default, which wins over the mode's rule. Block (`deny`) holds in
every mode; read-only calls no MCP tool; full access asks for nothing else
(Ask included). «Always» on an MCP tool's card (`allow_project`, also in a
loose chat; `approval.requested` says `grant_scope: "mcp_tool"`) saves Allow
for that tool instead of a project grant. Same connector, same destination:
after external content, a call that may send data out asks in project mode
(once or for this session, rule `external_content_send`) only when the
content came from somewhere else (a page, another connector); results of a
connector going back to that connector ask nothing, and Allow does not lift
this guard. Settings live in the server's `config_json` (`permissions
{default?, tools {<tool> {decision?, annotations?}}}`), survive `mcp.update`
and never hold a secret; changing them re-classifies the live tools without
reconnecting.
- `mcp.tools.list {name, scope?}` → `{server {name, scope, status, default,
  tools[]}}` from the server's last `tools/list` (nothing connects). Each
  tool: `{name, tool, title, description, listed, annotations {server, owner,
  effective}, kind (read | write | destructive), sends_out, setting, decision,
  decision_source (tool | server | mode), by_mode {read-only, workspace,
  full-access}}`; `by_mode` is what each mode does before any external
  content, and a saved setting for a tool the server no longer lists comes
  with `listed: false`.
- `mcp.tools.set {name, tool, decision?, annotations?, scope?}`: `decision`
  `allow` | `ask` | `deny` | `null` (back to the server's default);
  `annotations {readOnlyHint?, destructiveHint?, openWorldHint?}` with `true`,
  `false` or `null` (back to the server's hint) per key, or `null` to drop
  every correction. Marking a tool destructive also makes it a write. →
  `{server, tool}`. Anything else is `INVALID_PARAMS`; an unknown server is
  `NOT_FOUND`.
- `mcp.tools.set_default {name, decision, scope?}`: the server's Allow / Ask /
  Block for tools without their own setting (`null`: the mode decides) →
  `{server}`.

Connector catalog and imports (`mcp_catalog_v1`, plan de conectores C4).
The catalog is data shipped with the Engine (`src/rinari/mcp/catalog.json`,
validated against `catalog.schema.json`). `mcp.catalog.list` → `{version,
verified_at, entries}`; each entry: `id`, `name`, `logo` (an identifier: the
Engine ships no images), `category` (`google`, `work`, `dev`, `design`,
`files`, `crm`, `media`, `data`), `group?`, `preview?`, `description {es,
en}`, `notes? {es, en}`, `docs_url`, `tools?` (expected names, when known),
`server` (`{transport: "http", url, auth {kind: oauth | bearer, scopes?,
authorize_params?}}` or `{transport: "stdio", command, env [{name, label,
secret, required, default?}], connection_string? {schemes, env}}`), `setup
{kind, fields [{name, required}], console_url?, help {es, en},
alternatives?}` and `added` plus `servers [{name, scope, enabled, status}]`
(saved servers with the same URL — scheme, host and path — or the same
command, however they were added). `setup.kind`: `registration` (dynamic
client registration: nothing to type), `client_metadata` (Rinari's client
metadata document; alternative: an own `client_id`), `own_client`
(`client_id`, often `client_secret`, and `redirect_port` when the app needs
an exact redirect; Google, Slack, Asana, HubSpot), `token` (sent as Bearer;
GitHub, alternative: an own OAuth app) or `secrets` (a local server's
environment). `mcp.catalog.add {id, name?, client_id?, client_secret?,
redirect_port?, token?, secrets? {ENV: value}, connection_string?}` creates
the global server the entry describes (`name` defaults to the id, with `-2`,
`-3`… when taken; a taken `name` is `INVALID_PARAMS` with `details.hint`
`name_taken`, never a replacement) and returns `{server, entry_id, next}`:
`sign_in` (call `mcp.auth.start`) or `connect` (already connecting). A
missing required field, or one the entry does not use, is `INVALID_PARAMS`
naming the field, never its value. `connection_string` is split into the
entry's env names (`postgresql://user:password@host:port/db?query` →
`POSTGRES_*`; a path, `sqlite:///relative.db`, `sqlite:////absolute.db` or
`file:///C:/x.db` → `SQLITE_DATABASE`); every env value goes to the
credential store like any other.
`mcp.import.scan {sources?, json?}` lists, without creating anything, the
servers other apps on this PC declare — `claude_desktop`
(`%APPDATA%\Claude\claude_desktop_config.json` and the Microsoft Store
package, `~/Library/Application Support/Claude/…`,
`$XDG_CONFIG_HOME/Claude/…`), `claude_code` (`~/.claude.json` or
`$CLAUDE_CONFIG_DIR/.claude.json`: user servers and per-project ones),
`cursor` (`~/.cursor/mcp.json`), `vscode` (`Code` and `Code - Insiders`
`User/mcp.json` and `User/settings.json` → `mcp.servers`, JSON with
comments) and `codex` (`$CODEX_HOME/config.toml` or `~/.codex/config.toml`,
`[mcp_servers.*]`; with `RINARI_IMPORT_HOME`, for tests and sandboxes, that
folder stands in for `~` and `APPDATA`, `LOCALAPPDATA` and `XDG_CONFIG_HOME`
are ignored, so nothing of the owner's is read) — or the servers of a pasted `json` (`{"mcpServers":
…}`, `{"servers": …}` or a bare map). → `{sources [{source, label, path,
found, servers, error}], candidates [{key, name, source, source_label, path,
project_root, transport, command, url, secrets [{where: env | header | url,
name, kind: value | env_ref, variable? (the owner's environment variable an
env_ref reads: its name, never its value)}], warnings, importable, reason, existing,
suggested_name}]}`. Only names of secrets: values never leave the Engine.
`${VAR}` and `${env:VAR}` become `env://VAR`; `Authorization: Bearer
${VAR}` and Codex's `bearer_token_env_var` become bearer auth from that
variable; a URL's user and password become a Basic `Authorization` header;
secret-looking query values (`api_key`, `token`, …) keep the whole URL in the
credential store (`url_secret` in the view, `***` in `url`). Every imported
header value is stored as a secret. Not importable (`reason`):
`legacy_sse_unsupported`, `secret_in_command` (a secret on the command line
would stay in the row and the views: move it to `env`), `unresolved_variable`
(a variable in the URL), `invalid_url`, `invalid_definition`. Warnings:
`disabled_in_source`, `unresolved_variable:<field>` (that value is left
out: `${input:…}`, `${workspaceFolder}`…), `invalid_env_name`,
`cwd_ignored`, `env_file_ignored`. `existing` names a saved server with the
same definition. `mcp.import.apply {keys, names? {key: name}, sources?,
json?}` scans again (secrets never travel to the client and back) and
creates only the marked `keys`, each on its own, never replacing a server:
`{created [{key, server}], failed [{key, code, message}]}`. Claude Code's
per-project servers are created with project scope tied to their folder.
`mcp.project_offers.list {path}` → `{project, trusted, offers, answered,
files}`: the servers a trusted folder brings in `.mcp.json`,
`.rinari/mcp.json`, `.vscode/mcp.json` or `.cursor/mcp.json` (a name repeated
in a later file is skipped) that the owner has not answered, as candidates
(`key` is the name) with `changed` and `previous_decision`. An untrusted
folder offers nothing (`trusted: false`). `mcp.project_offers.decide {path,
decisions [{key, accept, name?}]}` records each answer for that folder
(`mcp_project_offers`, migration 0061), so each server is asked about once;
an accepted one is created with project scope tied to the folder
(`mcpServer.project_root`; it never runs in another project, even a trusted
one) and its secrets in the credential store. A definition that changes
later (command, URL without its query values, env or header names) is
offered again. → `{project, created [{key, server}], rejected, failed}`; an
untrusted folder is `INVALID_PARAMS` with `details.hint`
`project_not_trusted`, a key the folder no longer declares `NOT_FOUND`.
Project servers saved before 0061 (`project_root` null) stay visible in every
trusted project.
The envelope contract is unchanged across slices.

---


# Session Context Resolution Contract

The CLI must implement these exact semantics:

```text
rinari chat
  → CHAT always at entry

rinari
  → AUTO:
      detected project → PROJECT
      no project → CHAT

rinari agent/ask/plan/review/run
  → context-aware unless explicitly overridden
```

A session context may transition:

```text
CHAT → PROJECT
```

when the current task explicitly creates/adopts a project.

There is no automatic:

```text
PROJECT → CHAT
```

transition.

Project promotion is a runtime session operation, not deletion/recreation of the session.

The same session ID and conversation are retained.

---

# 13. Provider Registry

The provider system must support:

```text
multiple providers
multiple accounts for the same provider
login authentication
API-key authentication
environment secret references
local providers
custom providers
OpenAI-compatible providers
enterprise adapters
provider aliases
```

Potential adapters:

```text
OpenAI
Anthropic
Google
xAI
Mistral
Groq
OpenRouter
Azure OpenAI
AWS Bedrock
Google Vertex AI
Ollama
LM Studio
Custom OpenAI-compatible
Custom plugin adapter
```

Authentication choices must come from adapter capabilities rather than a hardcoded universal assumption.

---

# 14. `providers`

Plural command manages saved provider entries.

```text
providers list
providers add
providers login
providers logout
providers disable
providers enable
providers auth
providers show
providers test
providers rename
providers discover
providers import
providers export
providers remove
```

---

## `providers list`

```bash
rinari providers list
```

Example:

```text
ALIAS              TYPE        AUTH      STATUS       DEFAULT
openai-personal    openai      login     connected    *
openai-work        openai      api-key   connected
anthropic-work     anthropic   api-key   connected
google-personal    google      login     connected
local-ollama       custom      none      reachable
```

Options:

```bash
rinari providers list --connected
rinari providers list --type openai
rinari providers list --json
```

---

## `providers add`

```bash
rinari providers add
rinari providers add openai
rinari providers add anthropic --name anthropic-work
rinari providers add custom --name local-llm
rinari providers add --claude-subscription
```

`--claude-subscription` saves the experimental Claude Subscription provider,
which authenticates through the official Claude Code CLI instead of a
credential. It takes no endpoint, API key or protocol, and is refused unless
the CLI is installed and signed in through claude.ai
(`docs/providers/claude-subscription.md`).

Adding a provider never removes or replaces another provider unless the user explicitly selects update/overwrite behavior.

If alias already exists:

```text
Provider alias already exists.

  Update existing provider
  Create with another alias
  Cancel
```

Never overwrite credentials silently.

---

## `providers login`

```bash
rinari providers login openai-personal
```

Establishes login-based auth when adapter supports it.

If provider config already exists, only auth state changes.

It must preserve:

```text
provider record
endpoint settings
saved model aliases
provider default model
profiles referencing provider
```

---

## `providers auth`

Configure authentication method.

```bash
rinari providers auth anthropic-work --api-key
rinari providers auth openai-personal --login
rinari providers auth custom-prod --api-key-env CUSTOM_API_KEY
```

API keys should be saved as secret references, not plaintext config values.

---

## `providers logout`

```bash
rinari providers logout openai-personal
```

Semantics:

```text
provider remains saved
models remain saved
provider settings remain saved
authentication becomes disconnected
```

This is intentionally different from `providers remove`.

---

## `providers disable` / `providers enable`

```bash
rinari providers disable opencode-go
rinari providers enable opencode-go
```

Semantics:

```text
provider, models and credential remain saved
its models leave every picker (model.list hides them unless include_disabled)
no discovery, no usage reads and no model calls reach it
a turn that still asks for one of its models fails with PROVIDER_DISABLED
turning off the active provider leaves Rinari without one until another is used
```

---

## `providers show`

```bash
rinari providers show openai-personal
```

Show:

```text
immutable internal ID
alias
type
auth method
connection status
endpoint
account hint
capabilities
default model
last-used model
saved model aliases
created/updated timestamps
```

Secrets are always redacted.

---

## `providers test`

```bash
rinari providers test openai-personal
```

Checks:

```text
endpoint reachability
auth validity
model discovery where supported
minimal inference if needed
provider capability compatibility
```

---

## `providers rename`

```bash
rinari providers rename openai-personal openai-main
```

Alias changes must update live references safely while historical session provenance retains immutable provider IDs.

---

## `providers discover`

```bash
rinari providers discover
```

May detect:

```text
local Ollama
LM Studio
known local compatible endpoints
environment credential references
installed provider plugins
```

Discovery never auto-activates or stores secret values.

---

## `providers remove`

```bash
rinari providers remove anthropic-work
```

Explicit destructive registry command.

Must not delete:

```text
historical sessions
traces
unrelated providers
unrelated models
project data
```

If active:

```text
Provider is active.
Select another provider first or use:

  rinari providers remove anthropic-work --switch-to openai-main
```

Optional:

```bash
rinari providers remove anthropic-work --keep-credentials
rinari providers remove anthropic-work --revoke
rinari providers remove anthropic-work --dry-run
```

---

# 15. `provider`

Singular command means current provider.

```bash
rinari provider
rinari provider current
rinari provider show
rinari provider use anthropic-work
```

## Critical `provider use` invariant

Before:

```text
saved:
  openai-personal
  anthropic-work
  local-ollama

active:
  openai-personal
```

Command:

```bash
rinari provider use anthropic-work
```

After:

```text
saved:
  openai-personal
  anthropic-work
  local-ollama

active:
  anthropic-work
```

Nothing is deleted.

---

# 16. Provider-Specific Model Memory

Each provider should remember its own default/last-used model.

Example:

```text
openai-personal → gpt-main
anthropic-work  → opus
local-ollama    → qwen-coder
```

Then:

```bash
rinari provider use anthropic-work
```

restores:

```text
provider = anthropic-work
model    = opus
```

Later:

```bash
rinari provider use openai-personal
```

restores:

```text
provider = openai-personal
model    = gpt-main
```

This is preferable to one global model string that becomes invalid when the provider changes.

---

# 17. Model Registry

Models are saved independently from the active model.

Example:

```text
OpenAI
  gpt-main
  gpt-fast

Anthropic
  opus
  sonnet

Local
  qwen-coder
```

Switching never deletes aliases or settings.

---

# 18. `models`

Plural command manages model registry/catalog.

```text
models list
models available
models refresh
models add
models alias
models rename
models show
models test
models capabilities
models remove
```

---

## `models list`

```bash
rinari models list
```

Example:

```text
ALIAS       PROVIDER           MODEL ID         STATUS      DEFAULT
main        openai-personal    ...              available   *
fast        openai-personal    ...              available
opus        anthropic-work     ...              available
qwen        local-ollama       qwen3-coder      available
```

Options:

```bash
rinari models list --provider anthropic-work
rinari models list --saved
rinari models list --json
```

---

## `models available`

Query provider model discovery without saving models automatically.

```bash
rinari models available --provider openai-personal
```

Where known, show:

```text
reasoning
tool calling
vision
audio
structured output
parallel tool calls
context window
availability/deprecation
```

---

## `models pick`

Interactive provider + model selector (hermes model-style): choose a provider,
discover its real models grouped under it, and save + activate one in one step.

```bash
rinari models pick
rinari models pick --provider openai-personal
rinari models pick --provider openai-personal --name my-alias
```

Flow:

```text
1. choose a provider (menu of saved + "add a new provider", or --provider)
2. add a new provider from the built-in catalog (OpenAI, Anthropic, OpenRouter,
   DeepSeek, Groq, Together, Mistral, xAI, OpenCode Zen, OpenCode Go, Ollama,
   LM Studio, custom endpoint) -> pre-fills base URL + suggested env-var name
3. discover models from that provider (spinner while querying)
4. pick a model by number, or type a custom ID
5. save the model (if new) and activate it as the active model
```

Models already saved are marked `*`; picking one re-activates it without
duplicating. `--non-interactive` fails instead of prompting (pass
`--provider` + `--model` via `models add`/`model use` for scripting). With
`--json` it behaves like `models available`.

---

## `models refresh`

```bash
rinari models refresh
rinari models refresh --provider openai-personal
```

Refresh discovery metadata.

A model missing temporarily from discovery should be marked unavailable/unknown rather than deleting its saved alias.
The desktop refresh also saves the models a provider started offering
(`model.refresh` with `add_new`); the command keeps the saved list as is.

---

## `models add`

```bash
rinari models add \
  --provider openai-personal \
  --model <provider-model-id> \
  --name gpt-main
```

Custom/local:

```bash
rinari models add \
  --provider local-ollama \
  --model qwen3-coder \
  --name qwen
```

Wire transport (`chat` = `/chat/completions`, `responses` = `/responses`):

```bash
rinari models add \
  --provider opencode-go \
  --model muse-spark-1.3-contributor \
  --name muse13 \
  --transport responses
```

`models pick` sets `--transport responses` automatically for the OpenCode
models that live on `/responses` (Muse Spark contributors, Grok 4.5,
GPT 5.6 Luna); everywhere else the default is `chat`. An explicit
per-model setting always wins.

Streaming model calls have separate connection (15 s), first-byte (120 s),
inactivity (120 s) and total (900 s) defaults. Configure installation-wide
or provider-specific overrides in `model-execution.json` through:

```bash
rinari vision execution --first-byte 240 --idle 120 --total 1200
rinari vision execution --provider opencode-go --first-byte 300
rinari vision execution --provider opencode-go --inherit-timeouts
```

These are shared streaming settings despite the historical `vision execution`
command name. Agent exposes them under Settings → Advanced → Model execution.
Empty UI fields inherit. `--inherit-timeouts` clears the selected
provider's overrides, or the global overrides without `--provider`.

Precedence: installation → provider execution overrides → saved provider
`stream_timeouts` → saved model `stream_timeouts` → explicit request. Supported
keys are `connect`, `first_byte`, `idle`, `total`, in finite positive seconds.
The existing provider/model `stream_read_timeout_s` (1–600 s), and environment
fallback `RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS`, remain compatible and fill
first-byte/inactivity values not specified by the new policy. Direct adapter
callers without a policy retain the legacy 30-second read default.

A self-hosted provider — any custom endpoint, whatever its domain, or one on
this machine or the local network (localhost, a private address, `.local`) —
starts from longer defaults: first byte 600 s, inactivity 300 s, total 3600 s,
because a model the owner serves can read a long prompt for minutes before
answering. Known cloud APIs from the provider catalog keep the ordinary
defaults. Anything configured above still wins.

First-byte includes waiting for response headers; inactivity measures raw bytes,
including heartbeat traffic. The total bound also limits streams that send only
heartbeats. The agent loop makes a failed model call again after a short wait
when the failure is transient: a 5xx or overload, a rate limit, a cut stream or
a lost connection (two retries), or a timeout (one retry). Clients get
`model.retrying` and drop the text the failed attempt streamed. Tools are never
replayed: the call is retried before any tool runs on its answer. Each retry
counts as a model call. These settings do not change non-streaming call
deadlines; non-streaming calls keep the router's own retries.

---

## `models alias`

```bash
rinari models alias <provider-model-id> gpt-main \
  --provider openai-personal
```

---

## `models show`

```bash
rinari models show opus
```

Show:

```text
immutable internal ID
alias
provider
provider model ID
capabilities
availability
model-specific settings
provider-specific options
created/updated timestamps
```

---

## `models test`

```bash
rinari models test opus
```

Use the smallest meaningful request to validate access and expected capabilities.

---

## `models remove`

```bash
rinari models remove gpt-fast
```

Removes only the saved model record.

Must not remove:

```text
provider
credentials
other models
historical session metadata
```

If provider default is removed, request/select a replacement or fall back to automatic provider resolution.

---

# 19. `model`

Singular command means current model.

```bash
rinari model
rinari model current
rinari model show
rinari model use opus
rinari model reset
```

## `model use`

```bash
rinari model use opus
```

If `opus` belongs to another saved provider, the CLI may switch provider too, but it must say so explicitly:

```text
Active provider: anthropic-work
Active model:    opus
```

Nothing is deleted.

---

# 20. Provider + Model Resolution Order

When starting a task:

```text
1. command-level --provider / --model
2. session override
3. project override
4. selected profile override
5. active default provider
6. provider-specific default model
7. provider-specific last-used model
8. provider automatic recommendation
```

If `--provider` is specified without `--model`, resolve a model belonging to that provider.

Never attempt to pair a provider with an incompatible model from another provider.

---

# 21. Recommended Provider Record

```ts
type ProviderRecord = {
  id: string              // immutable internal ID
  alias: string           // user-facing, renameable
  type: string

  auth: {
    method: "login" | "api-key" | "none" | "custom"
    secretRef?: string
    accountHint?: string
  }

  endpoint?: string
  settings: Record<string, unknown>

  defaultModelId?: string
  lastUsedModelId?: string

  status?: {
    connected: boolean
    checkedAt?: string
  }

  createdAt: string
  updatedAt: string
}
```

Never use alias as primary key.

---

# 22. Recommended Model Record

```ts
type ModelRecord = {
  id: string
  alias: string
  providerId: string
  providerModelId: string

  settings: Record<string, unknown>

  capabilities?: {
    reasoning?: boolean
    tools?: boolean
    vision?: boolean
    audio?: boolean
    structuredOutput?: boolean
    parallelToolCalls?: boolean
    contextWindow?: number
  }

  availability:
    | "available"
    | "unavailable"
    | "unknown"
    | "deprecated"

  createdAt: string
  updatedAt: string
}
```

---

# 23. Custom Providers

```bash
rinari providers add custom
```

Fields:

```text
alias
adapter/protocol
base URL
auth method
headers
model discovery strategy
tool-call compatibility
streaming compatibility
provider-specific options
```

Common modes:

```text
OpenAI-compatible
Anthropic-compatible
local endpoint
plugin adapter
```

Example:

```bash
rinari providers add custom \
  --name lmstudio \
  --protocol openai-compatible \
  --base-url http://localhost:1234/v1
```

---

# 24. Credentials

Credential values live outside normal configuration.

Config stores references:

```yaml
auth:
  method: api_key
  secret_ref: keychain://rinari/providers/anthropic-work/api-key
```

Supported backends can include:

```text
OS keychain
encrypted local store
environment reference
1Password or vault connector
organization secret manager
```

Example env reference:

```bash
rinari providers auth anthropic-work \
  --api-key-env ANTHROPIC_API_KEY
```

Do not export plaintext secrets in normal config export.

OS keychain writes keep one service name per secret, scoped by home
(`rinari/<home-id>/providers/<id>`): two homes sharing an OS account never
collide, and a cleanup run from one home cannot touch another home's secrets.
A rotation writes the new value to a staging entry, copies the current value to
a previous entry, and only then replaces the definitive target, which is
deleted immediately before the write so no displaced copy is left behind. A
failure in any phase therefore leaves the previous value resolvable and the new
one staged for recovery; repeated writes do not accumulate orphaned entries.
`rinari secrets cleanup --apply` retires leftovers written by this home.

---

# 25. `config`

```text
config list
config get
config set
config unset
config edit
config path
config validate
config migrate
config diff
config reset
```

Examples:

```bash
rinari config get agents.max_total
rinari config set agents.max_total 12
rinari config unset context.compact_at_percent
rinari config edit --global
rinari config edit --project
rinari config validate
rinari config diff
```

`config set` validates schema before persistence.

`config reset` is explicit and should support `--dry-run`.

---

# 26. Configuration Layers

Recommended precedence:

```text
CLI flags                        highest runtime precedence
session override
project trusted config
selected profile
user config
organization/system config
built-in defaults               lowest
```

Locked security policies may not be overridden by lower layers.

Debug source:

```bash
rinari config get model.provider --explain
```

---

# 27. `profiles`

Capability/config profiles.

```text
profiles list
profiles show
profiles create
profiles clone
profiles edit
profiles use
profiles remove
```

Built-ins:

```text
safe
read-only
workspace
full-access
```

Examples:

```bash
rinari profiles use workspace
rinari profiles clone workspace my-workspace
```

Profiles may bundle model/provider preferences without modifying underlying provider/model records.

---

## `bundles` (Rinari profiles as workspaces)

Not the capability profiles above: a Rinari profile (soul, mode, per-agent
models) is where projects and conversations live, the same profiles the
desktop switches between (see "Rinari profiles as workspaces"). The active one
is shared with the desktop (`<home>/active_profile`); the app picks up a change
made here when its window regains focus.

```text
bundles list
bundles activate <id>
bundles create <id> --name NAME [--description D] [--soul ID] [--mode MODE]
               [--agent NAME=MODEL[,FALLBACK]]... [--activate]
bundles move-project <project-id | path> <id>
bundles move-session <session> <id> [--with-project]
```

- `list` shows each profile with its projects (not archived) and open
  conversations; `*` marks the active one (`--json`: `{profiles, active_id}`).
- `activate` makes new projects and conversations go to that profile.
- `create` validates `--agent`: a known agent and saved model aliases.
- `move-project` takes the project id or any path inside one of its folders,
  and moves the project with all its conversations.
- `move-session` moves a conversation that is not in a project. One inside a
  project has the project's profile: `--with-project` moves the whole project;
  taking it out of the project first is done in the app.

```bash
rinari bundles create trabajo --name "Trabajo" --agent explore=fast --activate
rinari bundles move-project ~/code/tienda trabajo
rinari bundles list
```

---

# 28. `project`

```text
project current
project info
project root
project list
project add
project remove
project instructions
project config
project index
project doctor
```

Useful:

```bash
rinari project current
rinari project instructions
rinari project instructions --show
rinari project instructions --explain
rinari project permission [read-only|workspace|full-access|none] [--path DIR]
```

`project permission` shows or sets the mode new conversations of the project
start in (none: Proyecto/workspace). A conversation keeps the mode it has;
asking for one when creating it wins over the default.

Instruction chain example:

```text
~/.rinari/RINARI.md
~/repo/RINARI.md
~/repo/src/RINARI.md
~/repo/src/payments/RINARI.override.md
```

Implementation (phase 3):

```text
resolver    rinari.instructions.resolver.resolve_project_instructions
chain       ~/.rinari/RINARI.md (global, user-owned, always trusted)
            then root -> ... -> cwd, deeper files take precedence
override    RINARI.override.md replaces RINARI.md at its own level
metadata    per file: scope (global|root|dir:<rel>), kind, trust, sha256, size
boundary    files are bounded to 32 KiB; README/other files are data,
            never instructions; untrusted projects contribute nothing
```

The `project instructions` subcommand surface above is not exposed yet; the
resolver above is shared by the prompt assembler and the trust gate.

---

# 29. `trust`

```text
trust status
trust list
trust add
trust remove
```

Example:

```bash
rinari trust add .
```

Trust can permit project-local:

```text
config
skills
hooks
MCP definitions
agent definitions
plugin declarations
```

Untrusted repositories may be read as data but must not silently activate executable agent configuration.

Implementation (phase 3):

Grant state, semantics, and revalidation:

```text
store         trust_entries (canonical_path PK, fingerprint, trusted_at, updated_at)
fingerprint   git directory identity + sorted remotes (git repos), .rinari/project.toml hash
              (marker projects), canonical path digest (plain directories)
states        trusted | not-trusted | revalidation-required | not-found
add           always captures a fresh fingerprint (re-grant after identity change)
```

Enforced runtime restrictions for untrusted projects:

- RINARI.md / AGENTS.md instructions are withheld from the prompt (the
  environment segment flags `project_trust` and the reason).
- `rinari init` auto-grants trust for the root it creates (explicit local act).
- Session start/resume warns when a project is untrusted or needs revalidation.
- Every agent session build persists a `ProjectTrustChecked` trace event.

---

# 30. `session`

```text
session current
session list
session show
session new
session resume
session rename
session pin
session unpin
session fork
session stop
session cancel
session archive
session export
session import
session delete
```

Examples:

```bash
rinari session new --name auth-refactor
rinari session resume ses_123
rinari session fork ses_123 --name alternate-approach
```

`session fork` (and `session.fork`) refuses a Rinari Agency conversation (an agent's
direct chat, a post or anything with `agent_id`): an agent works in its direct chat and
its posts. Its children can still start from a copy of it (`child.create` with
`context: fork`).

`session pin|unpin` marca una conversación como fijada (la sección «Fijados» del
escritorio). Es estado del Engine: el CLI y el escritorio ven la misma lista, y
`session list` antepone `[pin]` al título.

Resume must reconcile:

```text
project identity
git branch/state
working tree changes
permissions
provider availability
model availability
external assumptions
```

---

# 31. `tasks`

Expose the externalized task graph.

```text
tasks list
tasks show
tasks tree
tasks add
tasks update
tasks cancel
tasks retry
tasks blockers
```

Every subcommand takes `--project <dir>` (default: the current directory) and
the global `--json`.

| Command | What it does |
|---|---|
| `tasks list` | List the project's tasks. |
| `tasks show <task_id>` | Show one task with its done-when report. |
| `tasks tree` | Show the task graph as an indented tree, by dependency. |
| `tasks add <title>` | Add a task. Options: `--description`/`-d`, `--acceptance` (a checklist of `- [ ] ...` lines), `--implementation`, `--validation`, `--scope`, `--unresolved`, `--depends-on` (comma-separated task IDs). |
| `tasks update <task_id>` | Change fields: the same options as `add` plus `--title`/`-t`, `--status`, `--blocker` and `--evidence` (appends a semicolon-separated reference). |
| `tasks cancel <task_id>` | Cancel a task. |
| `tasks retry <task_id>` | Put a blocked or cancelled task back to `pending` and clear its blocker. |
| `tasks blockers` | Show tasks that are blocked or waiting on dependencies. |

Statuses: `pending`, `in_progress`, `done`, `blocked` and `cancelled`.
`--status` rejects anything else and enforces two gates (`tasks/service.py`):

- `in_progress` requires every dependency to be `done`;
- `done` requires the done-when contract (`tasks/core.py`): acceptance and
  validation criteria exist and are all checked (`[x]`), implementation and
  scope criteria (if any) are all checked, and nothing is left in
  `unresolved`.

Dependencies must be existing task IDs, and an edge that would close a cycle
is rejected. `tasks show` prints the done-when report with what is still
missing.

Example (`tasks list`):

```text
[done]         identify root cause
[done]         patch auth callback
[in_progress]  add regression test
[pending]      run integration tests
[pending]      inspect final diff
```

---

# 32. `context`

```text
context status
context inspect
context search
context retrieve
context pins
context pin
context unpin
context compact
context export
```

Expose model-visible context structure, not private chain-of-thought.

Useful information:

```text
active instruction segments
retrieved files
active skill
retrieved memory
artifact references
token budget
compaction status
```

---

# 33. `memory`

```text
memory list
memory search
memory show
memory add
memory edit
memory forget
memory clear
memory export
memory import
memory doctor
```

Scopes:

```bash
rinari memory list --user
rinari memory list --project
rinari memory list --episodic
rinari memory list --pattern
```

Deletion always requires explicit scope.

---

# 34. `artifacts`

```text
artifacts list
artifacts show
artifacts open
artifacts search
artifacts export
artifacts remove
artifacts gc
```

Examples:

```bash
rinari artifacts open artifact://session/.../pytest.log
rinari artifacts search "segmentation fault"
```

---

# 35. `checkpoint`

```text
checkpoint create
checkpoint list
checkpoint show
checkpoint restore
checkpoint remove
```

Examples:

```bash
rinari checkpoint create --name before-migration
rinari checkpoint restore chk_123 --dry-run
```

Restore must preview affected scope.

---

# 36. `undo` (phase 3: implemented)

```text
rinari undo                    restore latest checkpoint (agent-owned paths)
rinari undo create --project P --session S --label L
rinari undo list --project P
rinari undo preview --checkpoint ID [--allow-mixed]
rinari undo restore --checkpoint ID [--allow-mixed]
rinari undo remove ID
```

A checkpoint snapshots the working tree dirty state at create time (content
bytes per path, relative paths, 16MB cap per file). Each dirty path is
classified against the session's worktree baseline (captured at session
start):

```text
agent   dirty now, absent from baseline (the agent did it)
user    dirty before the session, unchanged since (the user's own work)
mixed   dirty before the session AND changed since (both owners)
```

Restore semantics are deliberately conservative:

- only `agent` paths are rewritten to their checkpoint content;
- `user` paths are never touched;
- `mixed` paths are reported and skipped unless `--allow-mixed` is passed
  (mixed ownership detection);
- a bare `rinari undo` (no subcommand) restores the latest checkpoint for
  the project; `--session` selects a different session (default: latest
  PROJECT session for the project root).

Undo operates only on local reversible agent-owned file changes. It does not
revert git commits, process side effects, or network calls, and it never
implies remote/external side effects are undoable.

---

# 37. `permissions`

```text
permissions show
permissions explain
permissions check
permissions grants
permissions revoke
```

Example:

```bash
rinari permissions check "git push origin main"
```

Possible output:

```text
Decision: prompt
Risk: high
Reason: remote git mutation
Matched policy: git.remote.push
```

---

# 38. `approvals`

```text
approvals list
approvals show
approvals revoke
approvals clear
approvals history
```

Possible scopes:

```text
once
session
project
persistent
```

Persistent grants should be conservative and fully inspectable.

---

# 39. `sandbox`

```text
sandbox status
sandbox profiles
sandbox show
sandbox test
sandbox exec
```

Example:

```bash
rinari sandbox test --write /tmp/example
```

Reports hypothetical allow/prompt/deny without performing the action.

---

# 40. `network`

```text
network status
network test
network rules
network allow
network deny
network remove
network history
```

Network policy remains authoritative regardless of model request.

| Command | What it does |
|---|---|
| `network status` | The active `network.mode`, how many rules there are (allow and deny) and the size of the audit trail. |
| `network test <target>` | Evaluate a host or URL against the current policy without touching it: prints `ALLOW`, `ASK` or `DENY` and the reason. |
| `network rules` | List the persisted rules. |
| `network allow <host> [--reason <text>]` | Persist an allow rule for a host and its subdomains. |
| `network deny <host> [--reason <text>]` | Persist a deny rule; it wins even with `network.mode=allow`. |
| `network remove <host> [--decision allow\|deny]` | Remove the host's persisted rules (both kinds by default). |
| `network history [--limit N] [--session <id>]` | Recent policy decisions, newest first (`--limit` 1–200, default 20). |

All of them accept the global `--json`. Rule changes apply to running
sessions without a restart.

Semantics (`policy/network.py`): decision per target host, in this order —
an explicit DENY rule always wins; `network.mode=off` denies everything; an
ALLOW rule allows; `network.mode=allow` allows the rest; with the default
`network.mode=auto` the session's permission profile decides (reads are free,
sending follows the profile); `network.mode=ask` routes every other target
through the approval gate. Rules match exact hosts and subdomains
(`github.com` covers `api.github.com`). A target that does not resolve to a
host is denied. Every runtime gate decision on a `network.outbound` tool call
is audited into `network history`.

---

# 41. `secrets`

```text
secrets list
secrets add
secrets remove
secrets rotate
secrets test
secrets scopes
secrets cleanup
```

Do not expose a plaintext-oriented `secrets show` command.

`secrets cleanup` reads the OS vault and classifies each entry as live (bound
to a provider that still exists), retained (kept on purpose through
`providers remove --keep-credentials`, recorded in its own retained_credentials table so it survives the provider row), orphaned (this home's namespace without
a live or retained provider), unknown (rinari-shaped but with no provable home
scope, such as the legacy shared service) or foreign (other applications). The
default is a dry run; `--apply` deletes only the orphaned entries and
revalidates each id against the database immediately before deleting it, so a
concurrent provider addition always wins. Additions, rotations and this cleanup share an exclusive per-home lock (credentials.lock), so a deletion phase cannot interleave with an addition whose secret is already written but whose row is not yet committed; --lock-timeout SECONDS (default 5) bounds the wait and a busy lock fails with CREDENTIAL_STORE_BUSY instead of touching the vault. Entries without a provable scope are
reported and never deleted. On systems without a native vault inventory the
command reports `unsupported` instead of failing.

Metadata is enough:

```text
configured: yes
backend: OS keychain
last updated: ...
scopes: provider:anthropic-work
```

---

# 42. `tools`

```text
tools list
tools search
tools show
tools load
tools unload
tools test
tools doctor
tools permissions
```

Examples:

```bash
rinari tools search postgres
rinari tools show shell.exec
rinari tools load github
```

Show:

```text
schema
risk
side effects
permission requirements
source
loaded state
```

---

# 43. `skills`

```text
skills list
skills search
skills show
skills activate
skills deactivate
skills install
skills import
skills remove
skills update
skills enable
skills disable
skills pending
skills approve
skills reject
skills revert
skills validate
skills test
skills create
skills path
```

Important distinction:

```text
install    persistent availability
activate   current session/task
disable    out of the catalog, kept on disk (Rinari's own skills too)
remove     deletion
```

Examples:

```bash
rinari skills activate fix-ci
rinari skills install https://github.com/acme/skills/tree/main/skills/pdf
rinari skills install ./pack.zip --name pdf-tools
rinari skills import                 # lists skills in ~/.claude, ~/.codex, ~/.agents
rinari skills import pdf-tools
rinari skills update pdf-tools       # from where it was installed
rinari skills disable debug
```

`install` accepts a folder, a `.zip`, a GitHub URL (repo or subfolder) or the
https URL of a single `SKILL.md`. It reads skills in the Agent Skills standard
(Claude, Codex) as well as Rinari's own format. Before copying anything it
reviews the content (prompt injection, `curl | sh`, exfiltration, destructive
commands, hidden Unicode, executables); when the review finds something it
shows it and asks once, and `--yes` accepts exactly the reviewed content.
`update` keeps local edits unless `--force`.

Learned skills: `/learn [focus]` (terminal or desktop) pins the packaged
`skill-author` skill and marks that turn as the owner's request; the skill it
proposes with `skills.propose` is saved active. Asking in plain words works too
("crea una skill por cada una"): the model passes the owner's words in
`owner_request`, and the Engine saves the skill active only when those words
are in a message the owner wrote in that conversation (a subagent looks in the
conversation that spawned it). Files, tool output, runtime notes, other agents
and scheduled runs never count, and a turn another agent started cannot borrow
them. A pending result says why in `pending_reason` (`needs_owner_approval`,
`owner_request_not_found` or `review_flagged`). An update of a learned skill is
also saved active in any turn: the owner is notified to review it, not to
approve it. A new skill Rinari proposes on its own (setting
`skills.auto_learn`, default `propose`), a change to an installed or
owner-created skill, and anything with dangerous review findings wait in
`rinari skills pending` until `approve` or `reject`. An approved change keeps an
installed skill's source and shows as a local modification. `revert` undoes
Rinari's last change: the previous version, or removed if it was a new learned
skill; an installed skill is never removed by it. Secrets are refused.

---

# 43b. `schedule`

```bash
rinari schedule list
rinari schedule add NAME --prompt TEXT (--at YYYY-MM-DDTHH:MM | --every MIN | --daily HH:MM | --weekly DAYS@HH:MM)
                    [--reminder] [--project ID] [--mode plan|build|review]
                    [--permission read-only|workspace|full-access] [--model ALIAS] [--allow CAPABILITY]...
rinari schedule enable|disable|remove ID
rinari schedule runs ID [--limit N]
```

Tasks run in the desktop app's Engine (open or in the system tray), not in
this command. `--allow` approves a capability in advance for every run;
anything else a run needs asks in the app. Days of `--weekly` are 0 (Monday)
to 6. Switching a task back on counts from now: missed runs are not caught up.
Without `--permission` a run starts in its project's default mode.

---

# 44. `agents`

```text
agents list
agents available
agents show
agents run
agents stop
agents logs
agents create
agents validate
```

Examples:

```bash
rinari agents run reviewer --task "review auth changes"
rinari agents run explore --task "map payment flow"
```

Direct agent execution is primarily for debugging/power users; the coordinator may spawn them automatically.

---

# 45. `plugins`

```text
plugins list
plugins search
plugins show
plugins install
plugins update
plugins enable
plugins disable
plugins remove
plugins permissions
plugins doctor
```

Install must preview requested capabilities.

Example:

```text
Plugin requests:
  network: api.github.com
  secrets: github.token
  hooks: PreToolUse
```

---

# 46. `mcp`

```text
mcp list
mcp add
mcp remove
mcp enable
mcp disable
mcp show
mcp connect
mcp disconnect
mcp tools
mcp resources
mcp prompts
mcp test
mcp logs
mcp login
mcp logout
```

Project-local MCP definitions require project trust.

Transports: `stdio` (a command) and `http` (Streamable HTTP; MCP 2025-06-18,
following the server down to 2025-03-26 or 2024-11-05):

```text
rinari mcp add github --url https://example.com/mcp --bearer          # token asked hidden
rinari mcp add github --url https://example.com/mcp --bearer-env GH_MCP_TOKEN
rinari mcp add api --url https://example.com/mcp \
    --header "X-Team: core" --secret-header X-Api-Key                   # value asked hidden
rinari mcp add local -- npx -y some-mcp-server
rinari mcp add notion --url https://mcp.example.com/mcp --oauth        # registers itself
rinari mcp add work --url https://mcp.example.com/mcp --oauth \
    --client-id <your client id> --client-secret --oauth-scope read    # your own OAuth app
rinari mcp login notion                 # signs in in your default browser
rinari mcp logout notion                # revokes (if the server can) and deletes the tokens
```

Tokens, secret headers and stdio env values never reach the database or the
config file: they are stored in the OS credential store (file fallback) or
kept as `env://NAME` references. `rinari mcp show` reports whether each one is
configured, never its value. `rinari mcp test` reports the failure code, the
HTTP status and a hint key (see the `engine` section, `mcp_remote_v1`). The
legacy HTTP+SSE transport is not supported.

OAuth (`--oauth`, see the `engine` section, `mcp_oauth_v1`): `--client-id`,
`--client-secret` (asked hidden) or `--client-secret-env VAR`, `--oauth-scope`
(repeatable), `--redirect-port` (a fixed loopback port, for OAuth clients that
need an exact redirect such as a Google "web" client) and
`--client-metadata-url` (a client ID metadata document). Without a client ID,
Rinari registers itself when the server allows it. `rinari mcp login <name>
[--no-browser] [--timeout 300]` prints the authorization URL on stderr, opens
the default browser and waits for the redirect to
`http://127.0.0.1:<port>/callback`; the tokens go to the credential store and
renew themselves. `rinari mcp show` says whether the server is signed in and
until when, never a token. `rinari mcp logout <name>` revokes and deletes them.

All MCP calls should still pass through Rinari policy, tracing, schema validation, and result normalization where possible.

---

# 47. `api`

OpenAPI/custom API integration.

```text
api list
api add
api remove
api show
api validate
api auth
api tools
api refresh
api test
```

Example:

```bash
rinari api add ./openapi.yaml --name internal-api
```

Generated operations become typed tools subject to normal policy.

---

# 48. `hooks`

```text
hooks list
hooks show
hooks enable
hooks disable
hooks test
hooks doctor
```

Untrusted project hooks do not execute.

---

# 49. `trace`

Primary observability command.

```bash
rinari trace
rinari trace current
rinari trace ses_123
```

Options:

```bash
rinari trace ses_123 --tools
rinari trace ses_123 --agents
rinari trace ses_123 --approvals
rinari trace ses_123 --validation
rinari trace ses_123 --prompts
rinari trace ses_123 --json
```

Do not expose hidden chain-of-thought. Show structured state, model-visible prompt segments, tool calls, policies, decisions, and evidence with secret redaction.

---

# 49b. `flow`

How a project (or one session) advanced, stage by stage: the same projection
the desktop shows (`flow.get`, `project_flow_v1`). Every value comes from
persisted turns, changesets, tasks and verification records; unknown values
print as `—`, never as an estimate.

```bash
rinari flow                     # registered project in the current directory
rinari flow --project ../api    # another registered project
rinari flow --session ses_123   # a single session (chat or project)
rinari --json flow --session ses_123
```

Stages are contiguous runs of turns **in one mode** across the sessions of
the scope, ordered by instant. PLAN / BUILD / REVIEW is how they are drawn,
not what separates them: `ask`, `agent` and `full-access` all draw as BUILD
and are different modes, so contiguous turns of those three are three
stages. A PLAN opens a cycle when it **starts**, so the cycle in progress is
visible while planning.

Progress is reported only where there is evidence. A build stage that
finished without task data prints `—`, not 100 %: that a turn ended says
nothing about how much of the work got done. The total prints `—` whenever
any stage is unknown, and the coverage says how many are known — a mean over
only the known stages used to report 100 % while a build was still running.
REVIEW is the share of checks that passed, not elapsed time.

Long histories are capped at 200 stages, most recent first, and the output
says when older ones were left out.

---

# 50. `logs`

```text
logs tail
logs show
logs search
logs export
logs clear
```

Examples:

```bash
rinari logs tail --runtime
rinari logs tail --mcp github
rinari logs tail --session ses_123
```

---

# 51. `eval`

```text
eval list
eval run
eval show
eval compare
eval report
eval create
eval validate
eval history
```

Examples:

```bash
rinari eval run coding
rinari eval run soul
rinari eval run safety
rinari eval compare run_123 run_124
rinari eval run coding --model opus
```

---

# 52. `metrics`

```text
metrics sessions
metrics models
metrics tools
metrics skills
metrics agents
metrics cost
metrics latency
metrics success
```

Examples:

```bash
rinari metrics models --last 30d
rinari metrics cost --project .
```

---

# 53. `cache`

```text
cache status
cache stats
cache clear
cache prune
```

Scopes:

```bash
rinari cache clear --web
rinari cache clear --repository-index
rinari cache clear --model-metadata
```

Cache and memory are distinct concepts.

---

# 54. `index`

Repository intelligence index.

```text
index status
index build
index update
index rebuild
index clear
index search
index doctor
```

Examples:

```bash
rinari index build .
rinari index search "authentication middleware"
```

---

# 55. `export`

```bash
rinari export config
rinari export session ses_123
rinari export skills
rinari export profile workspace
```

Secrets are not exported by default.

Secret references may optionally be exported, never secret values unless a separate explicitly privileged mechanism exists.

---

# 56. `import`

```bash
rinari import config ./config.bundle
rinari import session ./session.json
rinari import skill ./fix-ci
```

Validate:

```text
schema
version
permissions
plugin references
secret references
executable content trust
```

Imported executable code is not auto-trusted.

---

# 57. `update`

```bash
rinari update                 # plan, confirm, apply
rinari update --check         # report only; exit 1 when something is pending
rinari update --yes           # apply without asking
rinari update --desktop-only
rinari update --cli-only
```

The source is the latest **published release** of `Xainner/Rinari-Agent`.
Its `rinari-release.json` names the version and the Engine commit the app
ships. `RINARI_UPDATE_FEED` points to a folder or URL with the same files, for
tests and local rehearsals. Each piece is planned with its reason:

| Piece | How it is updated |
| --- | --- |
| Desktop app, closed | The installer is downloaded to `<home>/cache/updates`, verified by size and SHA-512, and run in updater mode (`--updated`). |
| Desktop app, open | `rinari-agent.exe --update`: the app asks with its own «Restart and update» dialog. |
| CLI bundled with the app | Moves with the app (it runs on the app's Engine). |
| CLI installed with uv from git | `uv tool install --force "rinari @ git+…@<engine_git_sha>"`. On Windows it runs after this command exits: uv cannot replace a running tool. |
| Editable CLI (a checkout) | Offers the same normal install; the checkout is never touched. |

`--json` without `--yes` only reports: applying asks, and a script cannot
answer.

Updates must preserve:

```text
providers
model aliases
credential references
profiles
sessions
memory
project trust
```

Config migration should be transactional where possible.

---

# 58. `completion`

```bash
rinari completion bash
rinari completion zsh
rinari completion fish
rinari completion powershell
rinari completion install
```

---

# 59. Interactive Slash Commands

Inside the interactive agent:

```text
/help
/status
/provider
/model
/mode
/plan
/tasks
/diff
/test
/verify
/review
/skills
/tools
/agents
/permissions
/checkpoint
/undo
/compact
/context
/trace
/cost
/new
/resume
/clear
/exit
```

Slash commands are a UI over the same application services, not a separate command implementation.

The list lives in one catalog (`rinari.commands`) shared with the desktop
composer (`command.list`); `/help` prints it. Kinds:

```text
/plan [text]     PLAN mode (read and plan). A mode change resumes the same
/build [text]    session rebuilt; the text, if any, is its first message.
/review [text]   REVIEW mode; alone, it reviews the uncommitted changes.
/test [text]     prepared turn: run the test suite (+ your text)
/skill <name> [text]   pin a skill and send the text
/<skill> [text]  every enabled skill is a command (a built-in keeps its name)
/tasks           the task tree (it was /plan before modes were commands)
/learn [focus]   save what was done here as a skill (saved active)
/lesson [focus]  save what the last turn taught: 1-3 rules into the skill
                 that does that job (or memory); never a new skill
/merge-skills a b [...]  merge the owner's skills into one (waits for approval;
                 the merged ones are turned off, undo turns them back on)
```

The Engine expands `mode`, `turn` and `skill` commands itself
(`session.turn.start` with `command: {name, text}`), so the terminal and the
desktop behave the same.

---

# 60. Interactive Provider Selection

```text
/provider

Select provider

  ● openai-personal
    anthropic-work
    google-personal
    local-ollama

  + Add provider
```

Selection:

```text
preserves every saved provider
preserves every credential reference
restores provider-specific model
changes session only by default inside an active session
```

Optional prompt:

```text
Apply to:
  This session
  This project
  Default
```

---

# 61. Interactive Model Selection

```text
/model

Provider: anthropic-work

Select model

  ● opus
    sonnet
    haiku

  + Discover models
  + Add model by ID
```

Show models for the active provider by default.

---

# 62. Missing Authentication

If switching to a saved but disconnected provider:

```text
Provider "openai-personal" is configured but not authenticated.

  Login
  Configure API key
  Cancel
```

Do not delete and recreate the provider.

---

# 63. Missing/Unavailable Model

If saved model is currently unavailable:

```text
Saved model "gpt-main" is currently unavailable.

  Choose another model
  Refresh model catalog
  Keep saved configuration and cancel
```

Never silently delete the model alias.

---

# 64. Project-Specific Provider/Model

Project config may specify preferred defaults:

```toml
[model]
provider = "anthropic-work"
model = "opus"
```

Set via:

```bash
rinari config set --project model.provider anthropic-work
rinari config set --project model.model opus
```

User can still temporarily override:

```bash
rinari --provider openai-personal --model gpt-main
```

No registry data is changed.

---

# 65. Profiles with Provider/Model Preferences

Example:

```toml
[profiles.fast]
provider = "openai-personal"
model = "gpt-fast"
sandbox = "workspace"

[profiles.deep-review]
provider = "anthropic-work"
model = "opus"
mode = "review"
```

Run:

```bash
rinari --profile deep-review
```

Underlying records remain unchanged.

---

# 66. CI / Non-Interactive Usage

```bash
rinari run "review this change" \
  --non-interactive \
  --provider ci-provider \
  --model reviewer \
  --profile read-only \
  --json
```

Non-interactive commands must never hang waiting for a TTY selection.

If information is missing, fail with a structured blocker/error.

---

# Visual CLI Command Contract

The command layer and interactive renderer share one runtime snapshot source.

Canonical sources:

```text
version          Build Manifest
provider/model   ProviderService + ModelService
reasoning        Model Router/provider metadata
tokens           UsageAccounting
context          Context Engine
cost             Usage/Cost Accounting
project/git      ProjectService + RepositoryState
permissions      Policy Engine
tools            Tool Registry
skills           Skill Runtime
agents           Agent Orchestrator
extensions       MCP/Plugin/Browser managers
completion       Completion Gate
```

## Visual flags

```text
--no-banner   suppress Rinari startup ASCII/card
--plain       disable color/cursor animation/live rail; retain readable text
--compact     request dense layouts
--no-color    disable ANSI colors
```

`--json` and `--json-stream` imply no banner and no ANSI.

## `/usage` and `/tokens`

Interactive aliases:

```text
/usage [session|today|project|all]
/tokens
```

`/usage` prints the same usage report the desktop paints in the chat
(`usage.report`, plan 0.4.0 #78), built from what the Engine stored — no model
call:

```text
Usage report · session · 2026-10-17T18:02:11Z
Engine 0.4.0 · profile Default · anthropic / sonnet

Plan limits:
  claude-sub · 5 h: 42% used · resets 2026-10-17T20:00:00+00:00
  openrouter · Credits: 12.40 USD

Context:
  31,204 of 200,000 tokens (16%) · 1 compactions

Session:
  sonnet: 17 calls · 412,812 input · 8,731 output · 318,304 cache read · 12,040 cache write · $0.4120
  Total: 17 calls · 412,812 input · 8,731 output · $0.4120

Tools: 38 (36 ok, 2 failed) · fs.read 14, shell.exec 9
Compactions: 1
Active time: 137 s
```

Plan limits come from the conversation's provider first (with why it reports
nothing, when it does not) and then from every other connected provider that
reports them; Claude Subscription's are the ones Claude Code gave with its last
answer. Input counts everything the model read, cached tokens included. Cost
uses the model's own prices, else the public catalog's (models.dev); a cache
rate it does not know is charged at the input rate. `/tokens` shows the
current context.

## `/model`

The model panel includes:

```text
provider alias
model alias
provider model ID
reasoning mode/effort
context window
max output when known
capabilities
token usage
cost when available
```

The UI may display reasoning **effort/mode and token accounting metadata**, but never hidden chain-of-thought or private model reasoning content.

---

# 67. Output Contract

Human output:

```text
concise
TTY-aware
stable enough for users
not intended for fragile parsing
```

Machine output:

```bash
--json
```

Recommended envelope:

```json
{
  "ok": true,
  "command": "providers.list",
  "data": {},
  "warnings": [],
  "metadata": {
    "timestamp": "..."
  }
}
```

Failure:

```json
{
  "ok": false,
  "command": "providers.test",
  "error": {
    "code": "AUTH_EXPIRED",
    "message": "Provider authentication has expired.",
    "retryable": false
  }
}
```

---

# 68. Exit Codes

Recommended stable codes:

```text
0   success
1   generic failure
2   invalid CLI usage
3   configuration error
4   authentication required
5   permission denied
6   approval denied
7   not found
8   conflict
9   validation/test failure
10  network failure
11  provider/model failure
12  tool failure
13  cancelled
14  partial completion
15  blocked
```

Document and version these carefully.

---

# 69. `--yes`

`--yes` may confirm expected low/medium-risk administrative prompts.

Reasonable:

```bash
rinari init --yes
rinari cache clear --web --yes
```

It must not universally bypass critical runtime policy for actions such as:

```text
production destruction
credential exfiltration
protected force push
critical remote mutation
```

---

# 70. `--dry-run`

Support wherever useful:

```bash
rinari providers remove old-provider --dry-run
rinari config reset --project --dry-run
rinari checkpoint restore chk_123 --dry-run
rinari plugins install foo --dry-run
```

Dry run must show intended state changes without applying them.

---

# 71. Provider Service Boundary

The CLI should not manipulate config files directly.

```ts
interface ProviderService {
  list(): Promise<ProviderRecord[]>
  get(ref: ProviderRef): Promise<ProviderRecord>
  add(input: AddProviderInput): Promise<ProviderRecord>
  update(id: string, patch: ProviderPatch): Promise<ProviderRecord>
  remove(id: string, options?: RemoveProviderOptions): Promise<void>

  authenticate(id: string, request: AuthRequest): Promise<AuthResult>
  logout(id: string): Promise<void>
  test(id: string): Promise<ProviderHealth>

  setDefault(id: string): Promise<void>
}
```

Critical invariant:

```text
setDefault() never calls remove()
```

Enforce this in service architecture, not only documentation.

---

# 72. Model Service Boundary

```ts
interface ModelService {
  list(filter?: ModelFilter): Promise<ModelRecord[]>
  discover(providerId: string): Promise<DiscoveredModel[]>
  add(input: AddModelInput): Promise<ModelRecord>
  remove(id: string): Promise<void>
  test(id: string): Promise<ModelHealth>

  setProviderDefault(
    providerId: string,
    modelId: string
  ): Promise<void>

  resolve(input: ModelResolutionInput): Promise<ResolvedModel>
}
```

Provider switching delegates model resolution to this service.

---

# 73. Provider Adapter Interface

```ts
interface ProviderAdapter {
  type: string

  supportedAuthMethods(): AuthMethod[]

  login?(request: LoginRequest): Promise<LoginResult>

  validateCredential(
    credential: CredentialHandle
  ): Promise<AuthStatus>

  listModels?(
    ctx: ProviderContext
  ): Promise<DiscoveredModel[]>

  invoke(
    request: ProviderModelRequest,
    ctx: ProviderContext
  ): Promise<ProviderModelResponse>

  capabilities(): ProviderCapabilities
}
```

The CLI derives authentication UX from the adapter.

---

# 74. Generic Command Handler Boundary

```ts
interface CommandHandler<I, O> {
  execute(
    input: I,
    ctx: CommandContext
  ): Promise<CommandResult<O>>
}
```

```ts
type CommandContext = {
  cwd: string
  interactive: boolean
  outputMode: "human" | "json"
  config: EffectiveConfig
  actor: ActorIdentity
}
```

```ts
type CommandResult<T> = {
  ok: boolean
  data?: T
  warnings?: Warning[]
  changes?: ChangeRecord[]
  error?: CommandError
}
```

This allows CLI, desktop, web, tests, and future APIs to reuse the same services.

---

# 75. Do Not Couple TTY to Business Logic

Bad:

```text
providers add
  prompts directly
  writes TOML directly
  writes keychain directly
```

Better:

```text
CLI parser
  ↓
ProviderService.planAdd()
  ↓
TTY gathers missing fields
  ↓
ProviderService.add()
  ↓
ProviderRegistry + CredentialStore
```

---

# 76. Command Audit Events

Mutating administrative commands should emit events such as:

```text
ProviderAdded
ProviderUpdated
ProviderRemoved
ProviderSelected
ProviderLoggedIn
ProviderLoggedOut
ModelAdded
ModelRemoved
ModelSelected
ProfileSelected
ProjectTrusted
ProjectUntrusted
PermissionGranted
PermissionRevoked
PluginInstalled
PluginEnabled
MCPAdded
MemoryForgotten
SessionDeleted
ConfigChanged
```

---

# 77. `dev`

Developer namespace for harness development.

```text
dev prompt
dev system-stack
dev tool-call
dev policy
dev events
dev db
dev migrate
dev fixtures
dev benchmark
```

---

## `dev system-stack`

```bash
rinari dev system-stack
```

Shows prompt segment metadata:

```text
01 constitution          trusted         stable
02 runtime-policy        trusted         session
03 soul                  trusted         stable
04 user-preferences      trusted         session
05 project-instructions  scoped-trusted  session
06 skill:fix-ci          scoped-trusted  session
07 task-state            trusted         turn
08 environment           trusted         session
09 environment-current   trusted         turn
```

`turn` segments are not part of the system prompt: they close each request
after the history, so the cached prefix survives a changed task graph or
repository scan.

Optional rendering:

```bash
rinari dev system-stack --render
```

Secrets remain redacted.

---

## `dev policy`

```bash
rinari dev policy explain \
  --tool shell.exec \
  --args '{"command":"git push origin main"}'
```

Expected:

```text
risk: high
side effect: remote-reversible
decision: prompt
matched rule: git.remote.push
```

---

## `dev events`

```bash
rinari dev events --session ses_123
```

Useful for replay, state reconstruction, eval generation, and debugging.

---

# 78. First-Run UX

Interactive:

```text
$ rinari

Rinari is not configured yet.
Run setup now? [Y/n]
```

Non-interactive:

```text
CONFIG_REQUIRED
Run: rinari setup
```

Do not silently create provider credentials or choose a provider account.

---

# 79. Daily Workflow

```bash
# first install
rinari setup

# project
cd ~/code/app
rinari init
rinari trust add .

# normal work
rinari "fix the failing auth tests"

# inspect
rinari status
rinari trace current

# later
rinari resume
```

---

# 80. Multi-Provider Workflow

```bash
# configure once
rinari providers add openai --name openai-personal
rinari providers add anthropic --name anthropic-work
rinari providers add custom --name local-ollama

# choose defaults
rinari provider use openai-personal
rinari model use gpt-main

# temporary alternate provider/model
rinari --provider anthropic-work --model opus \
  review "inspect this refactor"

# persistent switch later
rinari provider use anthropic-work

# switch back later
rinari provider use openai-personal
```

No credentials or model registrations are lost.

---

# 81. Provider Switching Acceptance Test

```text
Given:
  provider A configured with credential A
  provider B configured with credential B
  A default model = A1
  B default model = B1

When:
  rinari provider use A
  rinari provider use B
  rinari provider use A

Then:
  provider A exists
  provider B exists
  credential A reference exists
  credential B reference exists
  active provider = A
  active model = A1
  B default model remains B1
```

Keep this regression test permanently.

---

# 82. Model Switching Acceptance Test

```text
Given:
  model A1 saved
  model A2 saved

When:
  rinari model use A1
  rinari model use A2
  rinari model use A1

Then:
  A1 remains saved
  A2 remains saved
  both settings remain intact
  active model = A1
```

---

# 83. Logout Acceptance Test

```text
Given:
  provider openai-personal configured
  models main and fast saved

When:
  rinari providers logout openai-personal

Then:
  provider record remains
  models remain
  endpoint/settings remain
  auth status becomes disconnected
```

---

# 84. Remove Acceptance Test

```text
Given:
  A active
  B saved

When:
  rinari providers remove B

Then:
  B config is removed
  A is unchanged
  A remains active
  historical sessions referencing B remain readable
```

---

# 85. Non-Interactive Test Matrix

Every command should test:

```text
happy path
missing argument
unknown item
ambiguous alias
TTY behavior
non-interactive behavior
JSON output
permission failure
corrupt configuration
concurrent invocation
interrupted write
recovery
backward-compatible config
```

Provider/model commands additionally test:

```text
multiple accounts for same provider
login auth
API-key auth
custom endpoint
provider switch
model switch
switch back
provider-specific model restoration
expired authentication
unavailable model
remove inactive provider
attempt remove active provider
```

---

# 86. Recommended P0 Commands

Ship first:

```text
rinari
rinari setup
rinari help
rinari version
rinari doctor
rinari status

rinari ask
rinari plan
rinari agent
rinari review
rinari run
rinari resume
rinari stop
rinari verify

rinari provider
rinari providers
rinari model
rinari models

rinari config
rinari profiles

rinari init
rinari project
rinari trust

rinari session
rinari permissions
rinari approvals
rinari sandbox

rinari tools
rinari skills

rinari trace
rinari checkpoint
rinari undo

rinari completion
```

This is a strong single-agent CLI foundation.

---

# 87. Recommended P1 Commands

Add:

```text
memory
context
artifacts
index
logs
metrics

plugins
mcp
api
hooks

secrets
network

export
import
update
```

---

# 88. Recommended P2 Commands

Add:

```text
agents
tasks
eval
cost/more metrics shortcuts
advanced dev commands
```

Some can exist internally before becoming public UX.

---

# 89. Command → Harness Mapping

```text
setup
  → bootstrap/configuration

provider/providers
  → ProviderRegistry + CredentialManager

model/models
  → ModelRegistry + ModelRouter

config/profiles
  → ConfigurationEngine

init/project/trust
  → ProjectResolver + InstructionLoader

session/resume
  → SessionStore

tasks
  → TaskGraph

context
  → ContextEngine

memory
  → MemoryStore

artifacts
  → ArtifactStore

checkpoint/undo
  → Rollback subsystem

permissions/approvals
  → PolicyEngine

sandbox
  → SandboxRuntime

network/secrets
  → capability enforcement

tools
  → ToolRegistry + ToolRuntime

skills
  → SkillRuntime

agents
  → AgentOrchestrator

plugins/mcp/api
  → extension system

hooks
  → HookRuntime

trace/logs/metrics
  → Observability

eval
  → EvalRuntime

index
  → RepositoryIntelligence

doctor
  → health/integrity checks
```

---

# 90. Full Recommended Public Command Tree

```text
rinari
│
├── setup
├── help
├── version
├── doctor
├── status
├── init
│
├── ask
├── plan
├── agent
├── review
├── run
├── resume
├── stop
├── verify
│
├── provider
│   ├── current
│   ├── show
│   └── use
│
├── providers
│   ├── list
│   ├── add
│   ├── login
│   ├── logout
│   ├── auth
│   ├── show
│   ├── test
│   ├── rename
│   ├── discover
│   ├── import
│   ├── export
│   └── remove
│
├── model
│   ├── current
│   ├── show
│   ├── use
│   └── reset
│
├── models
│   ├── list
│   ├── available
│   ├── refresh
│   ├── add
│   ├── alias
│   ├── rename
│   ├── show
│   ├── test
│   ├── capabilities
│   └── remove
│
├── config
│   ├── list
│   ├── get
│   ├── set
│   ├── unset
│   ├── edit
│   ├── path
│   ├── validate
│   ├── migrate
│   ├── diff
│   └── reset
│
├── profiles
│   ├── list
│   ├── show
│   ├── create
│   ├── clone
│   ├── edit
│   ├── use
│   └── remove
│
├── project
│   ├── current
│   ├── info
│   ├── root
│   ├── list
│   ├── add
│   ├── remove
│   ├── instructions
│   ├── config
│   ├── index
│   └── doctor
│
├── trust
│   ├── status
│   ├── list
│   ├── add
│   └── remove
│
├── session
│   ├── current
│   ├── list
│   ├── show
│   ├── new
│   ├── resume
│   ├── rename
│   ├── fork
│   ├── stop
│   ├── cancel
│   ├── archive
│   ├── export
│   ├── import
│   └── delete
│
├── tasks
│   ├── list
│   ├── show
│   ├── tree
│   ├── add
│   ├── update
│   ├── cancel
│   ├── retry
│   └── blockers
│
├── context
│   ├── status
│   ├── inspect
│   ├── search
│   ├── retrieve
│   ├── pins
│   ├── pin
│   ├── unpin
│   ├── compact
│   └── export
│
├── memory
│   ├── list
│   ├── search
│   ├── show
│   ├── add
│   ├── edit
│   ├── forget
│   ├── clear
│   ├── export
│   ├── import
│   └── doctor
│
├── artifacts
│   ├── list
│   ├── show
│   ├── open
│   ├── search
│   ├── export
│   ├── remove
│   └── gc
│
├── checkpoint
│   ├── create
│   ├── list
│   ├── show
│   ├── restore
│   └── remove
│
├── undo
│
├── permissions
│   ├── show
│   ├── explain
│   ├── check
│   ├── grants
│   └── revoke
│
├── approvals
│   ├── list
│   ├── show
│   ├── revoke
│   ├── clear
│   └── history
│
├── sandbox
│   ├── status
│   ├── profiles
│   ├── show
│   ├── test
│   └── exec
│
├── network
│   ├── status
│   ├── test
│   ├── rules
│   ├── allow
│   ├── deny
│   └── history
│
├── secrets
│   ├── list
│   ├── add
│   ├── remove
│   ├── rotate
│   ├── test
│   └── scopes
│
├── tools
│   ├── list
│   ├── search
│   ├── show
│   ├── load
│   ├── unload
│   ├── test
│   ├── doctor
│   └── permissions
│
├── skills
│   ├── list
│   ├── search
│   ├── show
│   ├── activate
│   ├── deactivate
│   ├── install
│   ├── remove
│   ├── update
│   ├── validate
│   ├── test
│   ├── create
│   └── path
│
├── agents
│   ├── list
│   ├── available
│   ├── show
│   ├── run
│   ├── stop
│   ├── logs
│   ├── create
│   └── validate
│
├── plugins
│   ├── list
│   ├── search
│   ├── show
│   ├── install
│   ├── update
│   ├── enable
│   ├── disable
│   ├── remove
│   ├── permissions
│   └── doctor
│
├── mcp
│   ├── list
│   ├── add
│   ├── remove
│   ├── enable
│   ├── disable
│   ├── show
│   ├── connect
│   ├── disconnect
│   ├── tools
│   ├── resources
│   ├── prompts
│   ├── test
│   └── logs
│
├── api
│   ├── list
│   ├── add
│   ├── remove
│   ├── show
│   ├── validate
│   ├── auth
│   ├── tools
│   ├── refresh
│   └── test
│
├── hooks
│   ├── list
│   ├── show
│   ├── enable
│   ├── disable
│   ├── test
│   └── doctor
│
├── trace
│
├── logs
│   ├── tail
│   ├── show
│   ├── search
│   ├── export
│   └── clear
│
├── eval
│   ├── list
│   ├── run
│   ├── show
│   ├── compare
│   ├── report
│   ├── create
│   ├── validate
│   └── history
│
├── metrics
│   ├── sessions
│   ├── models
│   ├── tools
│   ├── skills
│   ├── agents
│   ├── cost
│   ├── latency
│   └── success
│
├── cache
│   ├── status
│   ├── stats
│   ├── clear
│   └── prune
│
├── index
│   ├── status
│   ├── build
│   ├── update
│   ├── rebuild
│   ├── clear
│   ├── search
│   └── doctor
│
├── export
├── import
├── update
├── completion
│
└── dev
    ├── prompt
    ├── system-stack
    ├── tool-call
    ├── policy
    ├── events
    ├── db
    ├── migrate
    ├── fixtures
    └── benchmark
```

---

# 91. Recommended Default Help Screen

```text
Rinari — AI engineering agent

Usage:
  rinari [prompt]
  rinari <command> [options]

Work:
  chat         Start explicit global chat mode
  ask          Ask about the project without modifying it
  plan         Investigate and produce an implementation plan
  agent        Run an autonomous coding task
  review       Review code without modifying it by default
  resume       Resume a previous session
  status       Show provider, model, project, and session state

Models:
  provider     Show or change the active provider
  providers    Manage providers and authentication
  model        Show or change the active model
  models       Manage and discover models

Project:
  init         Initialize Rinari for a repository
  project      Inspect project state and instructions
  trust        Manage trusted repositories

Runtime:
  permissions  Inspect effective permissions
  tools        Inspect and manage tools
  skills       Inspect and manage skills
  session      Manage sessions
  trace        Inspect agent execution

System:
  setup        Initial setup
  config       Manage configuration
  doctor       Diagnose Rinari
  update       Update Rinari
  help         Show help

Run `rinari help <command>` for details.
```

---

# 92. Production Command Completion Criteria

A command is not production-ready until it has:

```text
help text
examples
input validation
TTY behavior
non-interactive behavior
JSON output where relevant
stable exit code
side-effect classification
policy integration
audit event when mutating
tests
interruption handling
config migration behavior if applicable
```

---

# 93. Final Command Contract

The CLI must make these concepts impossible to confuse:

```text
SAVE
  add / create / install

SELECT
  use

TEMPORARY OVERRIDE
  --provider / --model / --profile / --mode

CONNECT
  login / connect

DISCONNECT WITHOUT DELETING
  logout / disconnect

PRESERVE BUT STOP USING
  disable / deactivate

DELETE
  remove / delete / clear / reset / forget / revoke
```

The user should be able to move freely between:

```text
OpenAI
Anthropic
Google
xAI
local models
custom endpoints
enterprise providers
multiple accounts
different models
different profiles
```

without ever worrying that changing the active provider or model destroys previous setup.

That persistence contract should be enforced in the service layer and covered by regression tests, not merely documented in CLI help.


## Visión: política Hermes (2026-09-12)

`rinari vision show` muestra la configuración compartida con Rinari Agent.
`rinari vision set automatic --model <modelo>` prioriza el auxiliar explícito.
`rinari vision set native` fuerza el principal; `rinari vision set auxiliary --model
<modelo>` utiliza el auxiliar. Los nombres anteriores `conversation` y `dedicated`
siguen siendo válidos y son los identificadores persistidos/protocolarios.

`rinari vision capability <modelo> supported|unsupported|auto` declara o elimina
una capacidad explícita. En automático, sin auxiliar, solo una capacidad verdadera
habilita la ruta nativa. Capacidad desconocida requiere configuración. No existen
confirmaciones visuales por mensaje: `--confirm-unknown` se acepta como obsoleto y
se ignora. Se mantienen las aprobaciones de acceso a archivos.


La política común de generación y concurrencia, incluida visión auxiliar, se
documenta en [Ejecución de visión](vision-execution.md).


## Context compaction controls

`rinari context settings` configures the shared threshold, optional summarizer and per-model window. `rinari context compact --session <id>` performs compaction without resuming the task. See [context-compaction.md](context-compaction.md) for the protocol, defaults and examples.

## Local dictation (`speech.*`)

The desktop composer can dictate: a microphone button, or Ctrl+Space held in
the message box. Transcription is local with whisper.cpp; audio never leaves
the machine.

- The CLI binary ships with the packaged Engine (`<engine>/speech/whisper-cli`,
  pinned in the Agent repo's `speech-manifest.json`, ~12 MB). `RINARI_WHISPER_BIN`
  overrides it with an absolute path to a file named `whisper-cli(.exe)`.
  Without either, the first `whisper-cli(.exe)` in an absolute `PATH` entry is
  used (never the current directory or a relative entry).
- A build from source has no bundled binary: set `RINARI_WHISPER_BIN`, put
  `whisper-cli` on the `PATH`, or run the Agent repo's
  `python scripts/package-speech.py` and point `RINARI_WHISPER_BIN` at
  `engine-dist\speech\whisper-cli.exe`. `speech.status` reports
  `binary_found: false` until one is found.
- Models are downloaded on first use into `~/.rinari/models/speech/`, checked
  against a pinned size and SHA-256 (`rinari.speech.catalog`): `base` (~60 MB),
  `small` (default, ~190 MB), `large-v3-turbo` (~575 MB).
- Settings (`speech.settings.set`): `model`, `language` (`""` = the client's UI
  language, `auto`, or a code) and `vocabulary` (names to spell right; "Rinari"
  is always included as Whisper's prompt).
- `speech.transcribe {audio: base64 WAV, language?}` takes mono 16 kHz 16-bit
  PCM, 0.2 s to 5 min, and returns a `job_id` at once; the text arrives as
  `speech.transcribed {job_id, text, model, language, audio_ms, elapsed_ms}` or
  `speech.failed {job_id, error}`. `speech.model.download` reports
  `speech.model.progress` and ends with `speech.model.ready` / `.failed`.
  Both run off the request loop so turns keep streaming.

Measured with `small` on a desktop CPU: 7.5 s of Spanish speech in ~2.8 s.

## Rinari profiles as workspaces (`rinari_profiles_v1`)

A Rinari profile (soul, mode, per-agent models) is also where projects and
conversations live. Projects and sessions carry `rinari_profile_id`
(migration 0047; everything older is in the built-in `default`). One profile is
active, shared by the CLI and the desktop (`<home>/active_profile`):

- `profile_bundle.list` → `{profiles, active_id}`; each profile adds `builtin`,
  `active` and `counts {projects, sessions}`. `default` always exists, can be
  edited (`profile_bundle.update {id, name?, description?, soul_id?, mode?,
  agents?}`, null clears) and never removed.
- `profile_bundle.active` → `{active_id, profile}`;
  `profile_bundle.activate {id}` → `{active_id, previous_id, profile}` and
  `profile_bundle.activated`. Its soul and mode are stamped on each new
  conversation (conversations of other profiles keep theirs).
  `profile_bundle.apply` remains for older clients.
- Per-agent models belong to the conversation: when a subagent starts, its
  model, fallback, effort and `enabled` come from the conversation's profile
  (`agents` map), field by field, over the global `agents.toml`; a profile with
  no entry for that agent uses the global one. Activating a profile changes
  nothing global, so two conversations of different profiles running at once
  (Boards) each use their own. `profile_bundle.create/update` reject an
  unknown agent or model alias (`NOT_FOUND`).
- New work goes to the active profile, or to `rinari_profile_id` in
  `session.create`, `project.add` and `project.open`. A project's conversations
  always have the project's profile (a different one is `INVALID_PARAMS`);
  moving a conversation into a project adopts the project's profile, and
  promoting a chat creates the project in the chat's profile.
- `session.list`, `project.list` and `project.list_recent` take
  `rinari_profile_id` (an id or `"active"`; omitted lists every profile).
- `project.move_profile {project_id, rinari_profile_id}` moves the project and
  all its conversations (`project.moved`). `session.move_profile {session_id,
  rinari_profile_id, project_policy?}` moves a loose conversation; one inside a
  project needs `project_policy`: `leave_project` (taken out first, same checks
  as `session.move`) or `move_project` (the whole project). Without it the
  error is `INVALID_PARAMS` with `details {code: PROJECT_POLICY_REQUIRED,
  project_id, project_name, project_session_count}`.
- `profile_bundle.remove {id, reassign_to?="default"}` reassigns its projects
  and conversations first (`profile_bundle.changed`); removing the active
  profile activates the target. Work whose profile file disappeared returns to
  `default` when the engine starts.
## Live checklist (`checklist.update`, `turn_checklist_v1`)

The model keeps a live list of the steps it is carrying out with the
`checklist.update` tool (the whole list each call, at most 20 items, statuses
`pending | in_progress | completed | blocked`; `blocked` needs a
`blocked_reason`; `items: []` clears it). Its description tells it to use the
list only for work with three or more steps and to mark an item completed only
after it is done. Subagents do not get the tool: the list is the main
conversation's.

Every change is turn activity `checklist.updated` (`reason`, `checklist`), one
card per turn (`checklist:{turn_id}`). The engine keeps it true:

- before the terminal event, a list the turn worked on is settled from the
  outcome: every item done → `completed`; a normal ending with items left →
  `open`; a cancelled, failed or stopped turn with items left → `interrupted`
  (`reason` `turn_completed`, `turn_cancelled`, …);
- turns a dead engine left behind settle as `interrupted` on restart
  (`reason` `engine_restart`);
- the next turn clears a `completed` list (`reason` `rollover`) and hands an
  `open` or `interrupted` one to the model in its task state, to reconcile;
- compaction keeps the items in the compact state's task lists.

`session.checklist.get {session_id}` → `{checklist|null}` (null once cleared).
`session.checklist.clear {session_id}` dismisses it; `TURN_RUNNING` while a
turn works on it. The CLI prints the list instead.

## Follow-up notes (`followup.suggest`, `followup_suggestions_v1`)

While it works, the model can leave the owner a note with a worthwhile task
outside the current request (`followup.suggest {title 3..80, prompt 10..2000,
rationale?}`; the prompt must be self-contained). A note never starts work.
At most 2 per turn, 3 pending per conversation and 5 per project (the oldest
pending gives way, `superseded`); a title like one from the last 30 days in the
same project (or conversation) is refused as a duplicate; pending notes expire
after 14 days. Subagents and turns started by another agent's message cannot
leave notes; a note records whether the turn had read outside content.

Events on the turn: `followup.suggested {suggestion}` and `followup.resolved
{suggestion_id, status, suggestion}`, one card per note (`followup:{id}`).

- `followup.list {session_id?, project_id?, rinari_profile_id?, status?="pending"}`
  → `{suggestions}`.
- `followup.dismiss {suggestion_id}` → `{suggestion}`.
- `followup.accept {suggestion_id}` → `{suggestion, session, turn, already_accepted}`:
  a new conversation in the same project and profile (same permission profile,
  BUILD, the note's title) whose first turn the engine starts with the prompt,
  `origin {kind: user, source: followup, suggestion_id, source_session_id}`.
  Accepting again returns the conversation already created.

## Projects with several folders (`project_folders_v1`)

A project has a primary folder (`canonical_root`, which still keys memory,
tasks, checkpoints and grants) and may own others (`project_folders`,
migration 0049; every existing project has its root as the primary). A folder
belongs to one project only.

- `project.folders.validate {paths, project_id?}` → `{folders: [{input,
  canonical_path, ok, error?: {code, message, project_id?, project_name?},
  trust_state, git_head}]}` with codes `NOT_FOUND`, `NOT_DIRECTORY`, `HOME`,
  `ENGINE_HOME`, `DUPLICATE`, `NESTED`, `IN_PROJECT`, `TOO_MANY` (16). Changes
  nothing: it backs the review step of the creation window.
- `project.create {name, description?, folders: [{path, trust}], rinari_profile_id?,
  open?=true}` → `{project, session|null, trust: [{path, state}]}`. All or
  nothing (`INVALID_PARAMS` with `details.folders`); the first folder is
  primary; `trust` grants each folder's trust after creation.
- `project.folder.add {project_id, path, trust?}`, `project.folder.remove
  {project_id, path}` (the primary is `CONFLICT`; a running turn in the project
  is `TURN_RUNNING`). `projectSummary.folders: [{path, primary, position,
  exists, git_head}]`.
- An extra folder is a working folder only when trusted: the sandbox can write
  there and the policy treats it as inside the project (writes, commands run
  there, delete targets). An untrusted one stays outside until trusted.
  The model gets the working folders in its environment (`project_folders`).
- Each trusted extra folder also brings its own configuration, under its own
  trust (an untrusted primary does not hide a trusted extra folder, nor the
  reverse):
  - its RINARI.md chain (from the folder down to the cwd when the cwd is inside
    it), labelled `folder <path>: ./RINARI.md` and prefixed with the folder it
    governs; `project.intelligence` lists those scopes with `folder`;
  - its `.rinari/hooks.json`: project hooks marked with `folder`, run only
    while that folder is trusted, and enabled/disabled together with project
    hooks of the same name (`rinari hooks list` shows the folder).
  MCP servers and plugins are not per folder: project-scoped MCP servers
  follow the primary folder's trust.
- Opening one of a project's folders opens that project; a CLI session started
  inside an extra folder binds to the project (its primary root, cwd kept).
  Checkpoints, LSP and the index stay on the primary folder.
