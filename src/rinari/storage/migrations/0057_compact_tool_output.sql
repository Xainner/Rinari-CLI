-- Plan 0.4.0 #91: the live output chunks of a tool that already ended are
-- dropped. The tool's terminal event carries its output, and the chunks only fed
-- the live view, and they were ~12 000 rows per heavy installation. Chunks of
-- a tool with no terminal event (a turn cut mid-run) are kept.
--
-- The ended tools are collected once into an indexed table: matching each
-- chunk against every event of its turn was quadratic in the turn's size, and
-- a turn with tens of thousands of chunks kept the Engine migrating past the
-- desktop's start limit, which killed it and started over (27-oct).
CREATE TEMP TABLE ended_tool_calls AS
SELECT DISTINCT session_id, turn_id, json_extract(payload_json, '$.tool_call_id') AS tool_call_id
FROM session_events
WHERE type IN ('tool.completed', 'tool.failed', 'tool.cancelled')
  AND turn_id IS NOT NULL;

CREATE INDEX temp.ended_tool_calls_key ON ended_tool_calls (session_id, turn_id, tool_call_id);

DELETE FROM session_events
WHERE type = 'tool.output.delta'
  AND turn_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM ended_tool_calls AS ended
    WHERE ended.session_id = session_events.session_id
      AND ended.turn_id = session_events.turn_id
      AND ended.tool_call_id = json_extract(session_events.payload_json, '$.tool_call_id')
  );

DROP TABLE temp.ended_tool_calls;
