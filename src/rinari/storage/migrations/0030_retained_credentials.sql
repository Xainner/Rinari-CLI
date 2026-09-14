CREATE TABLE retained_credentials (
  provider_id TEXT PRIMARY KEY,
  secret_ref TEXT NOT NULL,
  method TEXT NOT NULL,
  retained_at TEXT NOT NULL
);
