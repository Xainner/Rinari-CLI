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

Manual windows override automatic discovery. Automatic resolution uses normalized
endpoint metadata, saved model capability metadata and a destination-scoped cache.
The cache key includes provider ID, endpoint and provider model ID. Unknown windows use
128,000 tokens and are explicitly marked as estimates. Input and output contracts are
distinct; configured or protocol-required output budgets reduce usable input capacity.
No output cap is introduced by compaction.

Text accounting is an estimate, including system instructions, schemas and tool
descriptions. Subsequent requests can anchor that estimate to provider-reported usage.
This is not an exact tokenizer or a universal visual-token estimator. Image counts are
reported separately; existing media projection governs historical pixel retirement.

## Durable projection

Original session messages remain intact. The existing compact-state JSON is extended
additively with `projection_version`, cumulative `summary`, `revision`,
`history_revision` and `covered_message_ids`. Persistence precedes replacement of the
active projection. Reload excludes exactly those covered IDs, retaining newer messages
and the latest owner message. Tool calls and their results remain complete blocks.

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
- `context.status`: effective window and source for a saved model.
- `context.compact`: cancelable context-only operation on an existing session.
- `governor.compact`: stable `compaction_id`, reason, actual lifecycle state and accounting.
- Capability: `persistent_context_compaction_v1`.

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

Opt-in real-provider test:

```powershell
uv run python tests/manual/context_compaction_smoke.py --model SAVED_MODEL_ALIAS
```

The test copies only the selected provider configuration into a temporary home, uses
synthetic messages and never resumes a user session. OpenCode Go / Muse Spark 1.3 passed:
13,518 estimated tokens became 3,528; 19 messages were covered; the subsequent answer
retained the historical color decision. Elapsed time: 26.05 seconds. This is distinct
from deterministic tests and does not establish provider latency guarantees.

Validation on 2026-09-13: broad Engine suite 1,713 passed / 8 skipped; final context
regressions 15 passed; Agent frontend 92 passed; Rust 18 passed plus one opt-in packaged
Engine integration passed. Gateway frontend 18 passed; transport/context checks 9 passed,
and its context configuration restart test also passed against Agent's packaged Python.
Protocol generation/check and frontend builds passed. Packaged tool contracts and OCR
Spanish/scanned-PDF checks passed. No production Gateway deployment was performed.
