# Persistent context compaction

The session host prepares context before dispatching a conversation request, including
the first request after reopening a session. The shared configuration's
`runtime.safeguards.context_compaction` and `context.compact_at_percent` are authoritative;
the default threshold is 80 percent. The target is 60 percent of usable input capacity,
or 75 percent of a lower configured threshold.

The conversation model summarizes by default. `context-policy.json` contains only an
optional saved summarizer model ID and manual windows by saved model ID. It contains no
credentials. Provider transport, concurrency, output settings and timeouts remain owned
by the existing router. A summarization request exposes no executable tools.

## Window and accounting

Manual windows override automatic discovery and skip it. Otherwise each limit (total
window, maximum input, maximum output) is resolved on its own, from the first source
that states it: an explicit override saved on the model, then the endpoint's discovery,
then the bundled catalog (`providers/model_metadata.json`, a models.dev snapshot). When
none states a window, 128,000 tokens is used and marked as an estimate.

`context.status` (and the preflight that decides compaction, which uses the same
resolver) reports `window_source` — `manual`, `override`, `provider`, `catalog` or
`fallback` — for the limit that decides the effective window, plus each limit in
`max_context_tokens`/`max_input_tokens`/`max_output_tokens`, their sources in
`limit_sources`, and `discovered_at`/`metadata_updated_at` when an endpoint or the
catalog supplied them. A catalog value is never labelled as reported by the endpoint.

Discovery runs once per endpoint and is shared by every saved model on it; the cache
lives in `context-discovery-*.json` for one hour. `rinari models refresh` seeds it and
never waits for it to expire. A failed discovery is remembered for one minute as a
failure, never as data; offline, the last discovery saved for that model is used.

Input and output contracts are distinct. A maximum output is a limit, not a reservation:
only the request's or configuration's output budget reduces usable input capacity. No
output cap is introduced by compaction.

Text accounting is an estimate, including system instructions, schemas and tool
descriptions. Subsequent requests can anchor that estimate to provider-reported usage.
The anchor is persisted with the call that measured it (`ModelInvoked.context_anchor`),
because the desktop engine builds a new agent session every turn; it is restored only for
the same model and the same projection revision, and dropped after a compaction.

The compaction preflight, `context.status` and the CLI status line measure through the
same functions (`context/accounting.py`): same effective window, same output reserve,
same anchored estimate and the same compaction threshold and target.
This is not an exact tokenizer or a universal visual-token estimator. Image counts are
reported separately; existing media projection governs historical pixel retirement.

## Durable projection

Original session messages remain intact. The existing compact-state JSON is extended
additively with `projection_version`, cumulative `summary`, `revision`,
`history_revision` and `covered_message_ids`. Persistence precedes replacement of the
active projection. Reload excludes exactly those covered IDs, retaining newer messages
and the latest owner message. Tool calls and their results remain complete blocks.

Each compaction carries the previous structured state forward instead of re-deriving it
from the active tail: the goal persists until the user sets a new one explicitly (a
message starting with `Nuevo objetivo:`/`New goal:`), and constraints accumulate in order,
so a later instruction can correct an earlier one without the earlier one being lost.
Tasks, validations, approvals and blockers come from their records, not from the summary.

Before publishing, the summary is checked against those records. It cannot say that no
work is pending, or that the work is complete, when tasks or blockers are open or no
completed task is recorded; nor that tests passed without a recorded passing test run. A
contradiction gets one bounded repair request with the reason; a second one keeps the
previous projection. `governor.compact` reports what was checked in `checks`
(`structure`, `records`, `reduction`: `passed`, `repaired` or `failed`). These are
deterministic checks, not a proof of semantic fidelity.

Legacy rule-based states remain readable but do not imply a cut that was never saved.
The next necessary compaction creates a verifiable projection. Cancellation, an invalid
summary or a failed database write does not replace the previous projection. Summaries
are historical evidence and never grant approvals or tool permissions.

Only a structured provider context-overflow rejection can trigger a single reduced
request retry, and only before accepting output from that request. A timeout is not
treated as context overflow. Manual compaction uses the session turn lock and never
resumes the original task.

## Public interface

- `context.settings.get/set`: shared preferences; existing threshold configuration remains effective.
- `context.status {model_id}`: capacity of a saved model: effective window and its source,
  each limit and its source, usable input, output reserve, compaction threshold and target.
- `context.status {session_id}`: the same for the session's model, plus the last usage the
  provider reported for this projection (`last_request`), the projection revision and the
  last compaction (status, before/after, duration and `checks`). Rebuilt from persisted
  state, so it answers after a reload or an engine restart.
- `context.models {refresh?}`: every saved model's capacity in one call, sharing one
  discovery per endpoint; `refresh: true` drops the discovery cache first. A model that
  fails reports `error` without hiding the others.
- `context.compact`: cancelable context-only operation on an existing session.
- `governor.compact`: stable `compaction_id`, reason, actual lifecycle state and accounting.
- Capabilities: `persistent_context_compaction_v1`; `context_status_v2` for the session
  scope and `context.models`.

CLI examples:

```powershell
rinari context settings
rinari context settings --threshold 80
rinari context settings --summarizer conversation
rinari context settings --summarizer saved-model-alias
rinari context settings --model saved-model-alias --window 64000
rinari context settings --model saved-model-alias --window 0
rinari context compact --session SESSION_ID
```

Agent renders one compact activity, including failure/cancellation and an explicit
compaction retry. CLI progress stays out of JSON/protocol stdout. Gateway presents the
same engine event instead of maintaining its own compressor.

## Validation

Regression coverage includes pre-dispatch compaction, database reload, cumulative
summaries, incomplete summaries, cancellation, failed persistence, required-message
overflow, protocol settings and normalized provider contracts. UI tests verify one
operation identity and distinguish skipped/failed from completed.

The `context` eval suite (`src/rinari/evals/context_cases.py`, run by
`tests/unit/test_evals_context.py`) drives long scripted sessions through several real
compactions in a 40k window, with no network. It checks that the goal and a negative
rule survive repeated compactions, that an explicit "Nuevo objetivo:" replaces the goal,
that a corrected decision keeps its later value, that a summary claiming no pending
work is repaired against an open task, that tool calls and results stay paired, and
that the projection survives an Engine restart between turns. Disabling
`carry_forward` or the contradiction check makes the matching cases fail.

Opt-in real-provider test:

```powershell
uv run python tests/manual/context_compaction_smoke.py --model SAVED_MODEL_ALIAS
```

The test copies only the selected provider configuration into a temporary home, uses
synthetic messages and never resumes a user session. OpenCode Go / Muse Spark 1.3 passed:
13,518 estimated tokens became 3,528; 19 messages were covered; the subsequent answer
retained the historical color decision. Elapsed time: 26.05 seconds. This is distinct
from deterministic tests and does not establish provider latency guarantees.

Opt-in continuity comparison (costs money on the selected provider):

```powershell
uv run python tests/manual/context_continuity_live.py --dry-run
uv run python tests/manual/context_continuity_live.py --model SAVED_MODEL_ALIAS --max-calls 12 --out report.json
```

Each scenario asks the same question twice of the same model, once with the full
synthetic history and once after a forced compaction, and a rule judge checks both
answers. A scenario where the full answer passes and the compacted one fails is reported
as a regression. `--dry-run` prints the call count and an input-token ceiling without
reading credentials; a real run refuses a `--max-calls` below the minimum and stops
before any call would exceed it, keeping the report of what was already asked. A provider
or compaction error is recorded on its scenario and the run goes on. The report records
tokens, latency and the checks the compaction ran. `tests/unit/test_continuity_live.py`
exercises the same pipeline with a scripted model, including a compaction that loses the
state.

Baseline on 2026-09-23, OpenCode Go, 3 runs × 3 scenarios, 27 calls per model with no
repair needed. The token counts are estimates.

| Model | Full history | Compacted | Regressions | Compaction (tokens) | Compaction time |
| --- | --- | --- | --- | --- | --- |
| muse-spark-1.3-contributor | 9/9 | 9/9 | 0 | ~7.05k → 3.43k–3.55k | 5.0–9.9 s |
| deepseek-v4.1-flash | 9/9 | 9/9 | 0 | ~7.05k → 3.52k–3.80k | 6.4–16.4 s |

Every compaction passed its structure, records and reduction checks. The scenarios are
short and deliberately easy. They catch a compaction that drops the goal, a correction
or open work, but not subtler losses, and they do not establish latency guarantees.

Validation on 2026-09-13: broad Engine suite 1,713 passed / 8 skipped; final context
regressions 15 passed; Agent frontend 92 passed; Rust 18 passed plus one opt-in packaged
Engine integration passed. Gateway frontend 18 passed; transport/context checks 9 passed,
and its context configuration restart test also passed against Agent's packaged Python.
Protocol generation/check and frontend builds passed. Packaged tool contracts and OCR
Spanish/scanned-PDF checks passed. No production Gateway deployment was performed.
