CREATE TABLE pending_credential_cleanup (
  secret_ref TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  requested_at TEXT NOT NULL
);

CREATE INDEX idx_pending_credential_cleanup_provider
  ON pending_credential_cleanup (provider_id);
