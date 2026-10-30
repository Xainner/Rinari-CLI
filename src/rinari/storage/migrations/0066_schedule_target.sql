-- «Repetir aquí»: an agent task may run in the conversation it was asked in,
-- as one more message with its history, instead of a fresh session each time.
-- target is new_session (as before) or this_conversation, with its session_id.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE scheduled_tasks ADD COLUMN target TEXT NOT NULL DEFAULT 'new_session';
ALTER TABLE scheduled_tasks ADD COLUMN session_id TEXT;
