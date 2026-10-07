-- 0043: documentos (src/rinari/documents). Cada archivo que Rinari crea o
-- edita es una revisión inmutable con su padre, el original importado nunca
-- se sobrescribe. Los trabajos (crear, editar, renderizar, calcular…) tienen
-- estado persistido: un reinicio los deja `interrupted`, no en el limbo.
CREATE TABLE IF NOT EXISTS document_revisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    operation TEXT NOT NULL,
    backend TEXT,
    spec_uri TEXT,
    state TEXT NOT NULL DEFAULT 'draft',
    report_json TEXT,
    provenance_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_revisions_session
    ON document_revisions (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_document_revisions_document
    ON document_revisions (document_id, created_at);

CREATE TABLE IF NOT EXISTS document_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    progress_done INTEGER,
    progress_total INTEGER,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_jobs_session
    ON document_jobs (session_id, created_at);
