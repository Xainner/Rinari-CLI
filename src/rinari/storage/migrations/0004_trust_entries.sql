-- 0004: trust entries (phase 3, project trust).
-- An entry binds a canonical project path to a trust grant plus the identity
-- fingerprint captured at grant time. A fingerprint mismatch (or a missing
-- path) means the trust needs revalidation, not that it is silently active.

CREATE TABLE IF NOT EXISTS trust_entries (
  canonical_path TEXT PRIMARY KEY,
  fingerprint TEXT,
  trusted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);