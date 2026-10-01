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
in a temporary working directory, with `--system-prompt` replacing Claude
Code's own. `--bare` is deliberately never used: it forces `ANTHROPIC_API_KEY`
or `apiKeyHelper` authentication and never reads the subscription.

These environment variables are removed from the child only, because any of
them can move the call onto API or cloud billing:

```text
ANTHROPIC_API_KEY            CLAUDE_CODE_USE_BEDROCK
ANTHROPIC_AUTH_TOKEN         CLAUDE_CODE_USE_VERTEX
ANTHROPIC_BASE_URL           CLAUDE_CODE_USE_FOUNDRY
ANTHROPIC_BEDROCK_BASE_URL   CLAUDE_CODE_SKIP_BEDROCK_AUTH
ANTHROPIC_VERTEX_BASE_URL    CLAUDE_CODE_SKIP_VERTEX_AUTH
ANTHROPIC_API_KEY_HELPER
```

`provider.diagnostics.get` lists which ones it stripped; it never carries their
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
  `tool_calls: false` and refuses a request that carries tools instead of
  dropping them silently. Text turns work; a session that needs the filesystem
  does not.
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
