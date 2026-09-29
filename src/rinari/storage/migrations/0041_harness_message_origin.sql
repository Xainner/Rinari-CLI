-- 0041: runtime notes (loop detector, governor, returned subagent work) were
-- stored as plain user messages, so clients showed them as if the owner had
-- written them. New ones carry origin kind "harness", and this marks the ones
-- already stored. It only matches the exact prefixes the Engine itself wrote.
UPDATE session_messages
SET origin_json = '{"kind": "harness", "source": "loop-detector"}'
WHERE role = 'user' AND origin_json IS NULL AND content LIKE '[harness loop-detector] %';

UPDATE session_messages
SET origin_json = '{"kind": "harness", "source": "governor"}'
WHERE role = 'user' AND origin_json IS NULL AND content LIKE '[runtime governor] %';

UPDATE session_messages
SET origin_json = '{"kind": "harness", "source": "subagents"}'
WHERE role = 'user' AND origin_json IS NULL
  AND content LIKE 'Runtime: delegated work has returned. %';
