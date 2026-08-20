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
```

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
   DeepSeek, Groq, Together, Mistral, xAI, Ollama, LM Studio, custom endpoint)
   -> pre-fills base URL + suggested env-var name
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
rinari config get agent.max_turns
rinari config set agent.max_turns 200
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
```

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
fingerprint   git HEAD + sorted remotes (git repos), .rinari/project.toml hash
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

Example:

```text
[done]     identify root cause
[done]     patch auth callback
[running]  add regression test
[pending]  run integration tests
[pending]  inspect final diff
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

Semantics (phase 4): decision per target host — an explicit DENY rule always
wins; `network.mode=off` denies everything; an ALLOW rule short-circuits
`ask`; `network.mode=allow` allows the rest; the default `ask` routes targets
through the approval gate. Rules match exact hosts and subdomains
(`github.com` covers `api.github.com`). Every runtime gate decision on a
`network.outbound` tool call is audited into `network history`.

---

# 41. `secrets`

```text
secrets list
secrets add
secrets remove
secrets rotate
secrets test
secrets scopes
```

Do not expose a plaintext-oriented `secrets show` command.

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
skills remove
skills update
skills validate
skills test
skills create
skills path
```

Important distinction:

```text
install    persistent availability
activate   current session/task
remove     deletion
```

Example:

```bash
rinari skills activate fix-ci
```

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
```

Project-local MCP definitions require project trust.

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
rinari update
rinari update --check
rinari update --channel stable
rinari update --channel beta
rinari update --version 1.4.0
```

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
/usage
/tokens
```

They display normalized current-context and cumulative session usage:

```text
Usage
  context      31,204 / 128,000   24.4%
  turn         in 4,231  out 817  cached 2,044  reasoning 602
  session      in 42,812 out 8,731 cached 18,304 reasoning 6,104
  model calls  17
  tool calls   38
  elapsed      2m 17s
  cost         ~$0.184
```

Unsupported categories render as `—`.

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
06 skill:fix-ci          scoped-trusted  turn
07 task-state            trusted         turn
08 environment           trusted         turn
```

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
