# Provider subscriptions, capabilities and account usage

Local implementation, 2026-09-22. Experimental subscription compatibility is
not a public-vendor API guarantee. Real subscription acceptance and desktop
release pinning remain outstanding.

Engine owns the provider catalog, discovery metadata, credentials, OAuth
operations, transports, quota snapshots and continuation storage. Desktop
consumes `provider.catalog.get`, `provider.auth.start/get/cancel/logout`,
`provider.usage.get`, `provider.diagnostics.get` and `model.capabilities`.
Feature negotiation uses `provider_catalog_v1`, `provider_usage_v1` and
`provider_subscription_auth_v1`. Auth and usage update events contain no tokens.

`providers/metadata.py` is shared with context window resolution. Endpoint
metadata wins over the bundled models.dev snapshot; explicit saved overrides
win over both. Refresh the bundled snapshot for a release using
`uv run python scripts/update-provider-metadata.py`, then review the diff.
Unknown or unimplemented transports fail before invocation. Advertising a model
capability alone does not enable an adapter feature.

Signed continuation blocks are stored with the original provider/endpoint/model
identity and survive restart and session fork. They are excluded from public
message snapshots and exports. Migration 0033 adds their storage; migration 0034
indexes provider-identified usage events in the existing session database.

Account quota adapters cover OpenCode Go, OpenRouter, DeepSeek and ChatGPT.
Unknown numbers stay null; account credits and key limits are separate. Cached
queries honor Retry-After and exponential backoff, and expose fetched/retry
timestamps. Inference quota rejection invalidates the successful-cache interval
without making inference wait for another remote usage request. Consumers poll
at most once per minute while visible. Unsupported products link to their panel.
Local usage only counts recorded model events with provider identity; legacy
history and unrecorded auxiliary calls are intentionally not attributed.

ChatGPT login uses external-browser PKCE or device flow. Refresh tokens live in
the Engine credential store; per-provider file locks serialize renewal across
processes. An authentication rejection may be retried once before output starts.
Copilot uses device flow and its catalog's supported endpoints, with specific
headers for Chat/Responses/Messages. Logout only removes this Rinari connection.
No fallback billing product, account rotation, purchase or reset redemption.

Validation is deterministic with mocked HTTP, including callback state,
occupied port, expiration, concurrent renewal, cancellation, quota isolation,
decimal balances, tool signatures and transport selection. A real Go usage
query succeeded without inference. ChatGPT/Copilot login and paid inference
were not exercised with real accounts. See the desktop repository's
`docs/providers-subscriptions-review-plan.md` for the acceptance matrix.
