-- Plan 0.4.0 #40 (child sessions, phase A1): which conversation a session came from.
-- lineage_kind is note (an accepted followup note), fork or schedule (a scheduled run,
-- which has no parent session). brief_json keeps the title and the request.
-- NOTE: no semicolons in comments (the runner splits on them).
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT;
ALTER TABLE sessions ADD COLUMN lineage_kind TEXT;
ALTER TABLE sessions ADD COLUMN brief_json TEXT;
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
UPDATE sessions
   SET parent_session_id = forked_from,
       lineage_kind = 'fork'
 WHERE forked_from IS NOT NULL AND parent_session_id IS NULL;
UPDATE sessions
   SET parent_session_id = (
           SELECT f.session_id FROM followup_suggestions f
            WHERE f.accepted_session_id = sessions.id ORDER BY f.resolved_at LIMIT 1),
       lineage_kind = 'note',
       brief_json = (
           SELECT json_object('title', f.title, 'request', f.prompt, 'suggestion_id', f.id)
             FROM followup_suggestions f
            WHERE f.accepted_session_id = sessions.id ORDER BY f.resolved_at LIMIT 1)
 WHERE parent_session_id IS NULL
   AND EXISTS (SELECT 1 FROM followup_suggestions f WHERE f.accepted_session_id = sessions.id);
UPDATE sessions
   SET lineage_kind = 'schedule',
       brief_json = (
           SELECT json_object('task_id', r.task_id, 'run_id', r.id, 'title', t.name, 'request', t.prompt)
             FROM scheduled_runs r LEFT JOIN scheduled_tasks t ON t.id = r.task_id
            WHERE r.session_id = sessions.id LIMIT 1)
 WHERE lineage_kind IS NULL
   AND EXISTS (SELECT 1 FROM scheduled_runs r WHERE r.session_id = sessions.id);
