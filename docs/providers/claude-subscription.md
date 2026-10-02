# Claude Subscription (experimental)

Use a Claude plan from Rinari through the **official Claude Code CLI**. Rinari
stays the agent; Claude Code is only an authenticated inference transport.

Verified against Claude Code **2.1.286** on Windows 11, 2026-09-30.

```text
Rinari Agent -> Rinari Engine -> ClaudeSubscriptionAdapter -> ClaudeCliRuntime
             -> request-scoped `claude` process -> Anthropic
```

## Requirements

- The official Claude Code CLI, installed by the user. Rinari does not bundle,
  install or update it.
- Claude Code signed in through **claude.ai** (`claude auth login --claudeai`).
  A Console/API login, Bedrock, Vertex or Foundry is refused on purpose.

Rinari finds the binary in this order: a saved `command_path` override, the
`RINARI_CLAUDE_COMMAND` environment variable, `PATH`, then the paths the
official installers use. The Windows installer writes `~/.local/bin/claude.exe`
without adding it to `PATH`, and a macOS Electron app inherits a GUI `PATH`
without the user's shell additions, so the last step is not a nicety.

## Security boundary

| Rinari does | Rinari never does |
|---|---|
| Read `claude auth status --json` | Read `~/.claude`, tokens or credential files |
| Spawn one `claude` process per model call | Store a Claude credential in `CredentialStore` |
| Strip billing overrides from the child environment | Modify the user's own environment |
| Run the child in a throwaway directory | Let Claude Code read the workspace |
| Keep history in the Rinari session | Use Claude Code's saved sessions |

The child starts with `--tools ""`, `--disable-slash-commands`,
`--setting-sources ""`, `--strict-mcp-config` and `--no-session-persistence`,
in a temporary working directory. Rinari's system prompt replaces Claude
Code's own and travels as `--system-prompt-file`, never as an argument: it runs
well past the 32 KB Windows command line, and an argument would also expose it
in the process list. `--bare` is deliberately never used: it forces
`ANTHROPIC_API_KEY` or `apiKeyHelper` authentication and never reads the
subscription.

Probes run with `stdin` closed. The Engine speaks NDJSON over its own stdin,
and `capture_output` redirects only stdout and stderr, so an inherited stdin
let a probe's child swallow protocol bytes and time out every request.

Every `ANTHROPIC_*`, `CLAUDE_*` and `CLAUDECODE` variable is removed from the
child, and only from the child. Two different leaks make this necessary:

- **Billing.** `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY_HELPER` and the
  `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY` switches move the call off
  the subscription and onto an API or cloud bill.
- **Identity.** The `CLAUDE_CODE_*` family carries another Claude Code
  session's messaging token, OAuth scopes, account and organization ids. The
  first real smoke caught all of them being inherited whole when Rinari itself
  ran inside a Claude Code session.

The child authenticates from the user's own `~/.claude`, reached through `HOME`
and `USERPROFILE`, so it needs none of them. Rinari sets
`CLAUDE_CODE_ENTRYPOINT=rinari` after the sweep and nothing else.

A side effect worth knowing: configuration the user passes through those
variables (for example `CLAUDE_CONFIG_DIR`) does not reach this transport.
That is deliberate for an isolated transport, not an oversight.

`provider.diagnostics.get` lists the names it stripped; it never carries their
values.

## Authentication states

`claude auth status --json` is read when the provider is created and again
before every request, with no long-lived "connected" cache: an external
`claude auth login --console` between two turns must be noticed.

| State | Meaning |
|---|---|
| `missing_cli` | The binary was not found |
| `unsupported_cli` | Older than 2.1.0, or a known-bad version |
| `logged_out` | Installed, not signed in |
| `connected` | `loggedIn` and `authMethod == "claude.ai"` |
| `non_subscription_auth` | Signed in through Console/Bedrock/Vertex/Foundry |
| `error` | The CLI answered with something unreadable |

Only `connected` runs. Anything unknown fails closed: `subscriptionType` is
used as a label for the plan but never decides the verdict, because its shape
has changed between CLI versions.

## Setup

Desktop: Settings → Providers → Claude Subscription. The card reports the state
above and only offers **Connect** when the CLI is on a subscription.

Terminal:

```bash
rinari providers add --claude-subscription
```

It takes no endpoint, credential or protocol; passing one is an error rather
than a silently ignored argument.

## Models

Claude Code offers no print-mode command that lists the models a plan
includes, so Rinari exposes the aliases the CLI documents for `--model`
(`fable`, `opus`, `sonnet`, `haiku`) with availability **unknown**, and records
the concrete model the CLI resolved (for example
`claude-sonnet-4-6-20260219`) from the response. Nothing claims the account has
a model until a call proves it, and no context window is invented: it stays
`null` until a source states one.

Models of this product are listed separately from Anthropic API models. They
may be the same model, but they are different auth and billing routes, so
Rinari persists `provider_id`, `product_id`, `provider_model_id` and
`transport` rather than a name.

## How the real CLI behaves (verified against 2.1.286)

Checked with a real subscription, launching the CLI exactly as the transport
does. Each point below changed the implementation.

- **One `assistant` event per content block.** A reply with thinking and text
  arrives as two `assistant` events sharing one message id; Haiku with no
  effort set already thinks. Single-request admission therefore counts
  message ids, not events -- counting events cut every such reply off before
  its `result`, which is the only place the reported usage comes from.
- **Errors can say `subtype: "success"`.** A model the plan does not cover
  answers with an out-of-credits notice as an assistant message, then a
  `result` with `subtype: "success"`, `is_error: true` and
  `api_error_status: 429`. `is_error` is the verdict, and assistant-message
  text is held back until the `result` confirms it was an answer, so a notice
  is never streamed to the user as the model's reply.
- **The billing risk is real, and `auth status` does not see it.** With
  `ANTHROPIC_API_KEY` reaching the child, the CLI reports
  `apiKeySource: "ANTHROPIC_API_KEY"` and bills the API, while
  `claude auth status` still says `claude.ai`. With the transport's
  sanitized environment the same call reports `apiKeySource: "none"` and runs
  on the subscription. The environment sweep is what protects the user; the
  auth check alone would not.
- **Isolation holds.** The init event shows no tools, no MCP servers and no
  slash commands, and the user's own `~/.claude/CLAUDE.md` does not reach the
  child. Auto-memory points at an empty directory derived from the temporary
  working directory, and no folder is left behind in `~/.claude/projects`.
- **Aliases resolve.** `opus`, `sonnet` and `haiku` resolve to concrete
  models (for example `claude-opus-5-5`); `fable` may need usage credits a
  plan does not include, and fails with the 429 above.
- **`ultracode` is not an effort level.** `--effort ultracode` is accepted
  without a warning, but in the CLI it is a mode that runs dynamic workflows
  and agents of its own while the effort stays as it was. That would turn
  Claude Code into a second agent inside Rinari (plan sections 3.1 and 13),
  so Rinari does not offer it. `max` is the highest level.

## Authentication from Rinari

`provider.auth.get` reads the CLI live and answers `connected`,
`needs_auth` (signed out) or `error` (any other source), with
`auth_kind: "external-cli"` and `managed_by: "claude-cli"`. Rinari runs no
login of its own: `provider.auth.start` answers with the command to run, and
`provider.auth.logout` refuses, because `claude auth logout` would sign the
user out of Claude Code everywhere. Disconnecting from Rinari is removing the
provider.

A binary override saved in provider settings (`command_path`) must be an
existing file named `claude`, `claude.exe` or `claude.cmd`. Provider settings
are writable from the desktop, so anything else is refused when saved and
ignored if it appears later; `provider.runtime.probe` takes no path at all.

## Usage and limits

`provider.usage.get` reports `supported: false` for this product: there is no
official contract for reading a subscription's remaining allowance, and Rinari
does not scrape claude.ai. Token counts reported by the CLI are recorded as
usage, never converted into a bill.

> Usage counts against the limits of the Claude account currently signed in
> through Claude Code. Availability and limits are controlled by Anthropic and
> may change.

## Limits of this version

- **No tools.** Until the Rinari tool bridge lands, the provider announces
  `tool_calls: false`. The agent runtime honours that and offers the model no
  tools, so an ordinary text turn completes; the adapter still refuses a
  request that arrives carrying tools, rather than dropping them silently. A
  session that needs the filesystem cannot use this provider yet.
- **Reasoning effort** is forwarded only for the five levels `--effort` takes
  (`low`, `medium`, `high`, `xhigh`, `max`). Rinari offers three more
  (`none`, `minimal`, `ultra`); the CLI answers an unknown value with a
  warning on stderr and falls back to its default, so Rinari does not send
  them. Picking one of the three leaves the model on its own default effort.
- **Thinking** blocks are kept on the response as items with their
  signature, the same way the HTTP adapter keeps them. Nothing renders them:
  Rinari does not display a model's private reasoning (AGENTS.md), so what the
  user controls and sees is the effort level, not the thinking. Whether a turn
  produces any is the model's and the CLI's decision.
- **No vision**, no structured output, no continuation reuse.
- **Concurrency** is not yet limited per account.
- The CLI has no `--max-turns` in 2.1.286, so a single generation per Rinari
  turn is enforced locally: the first assistant generation is admitted and any
  later one ends the process.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Claude Code is not installed, or Rinari cannot find it" | Not installed, or in a location not covered; set `RINARI_CLAUDE_COMMAND` |
| "Claude Code is not signed in" | Run `claude auth login --claudeai` |
| "non-subscription source" | The CLI is on Console/Bedrock/Vertex; sign in again with `--claudeai` |
| "exited without producing a response" | A print-mode regression in that CLI version; `claude update` |
| "cannot run tools yet" | Expected: use another provider for tool work |

## Disconnecting

Removing the provider in Rinari removes only Rinari's entry. It never runs
`claude auth logout`, because that would sign the user out of Claude Code
everywhere, including their terminal and other applications.
