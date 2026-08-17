-- Phase 4: context pins. A pin is a session-scoped pointer to model-visible
-- context that must stay injected regardless of retrieval ranking
-- (harness: context pins). Sources: file, symbol, memory, artifact, term.
-- NOTE: this file must not contain semicolons inside comments because the
-- migration runner splits statements on the semicolon character.

CREATE TABLE IF NOT EXISTS context_pins (
    session_ref TEXT NOT NULL,
    source TEXT NOT NULL,
    pin_ref TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_ref, source, pin_ref)
);

CREATE INDEX IF NOT EXISTS idx_context_pins_session
    ON context_pins(session_ref);