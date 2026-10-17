-- Plan 0.4.0 #91: the live output chunks of a tool that already ended are
-- dropped. The tool's terminal event carries its output, and the chunks only fed
-- the live view, and they were ~12 000 rows per heavy installation. Chunks of
-- a tool with no terminal event (a turn cut mid-run) are kept.
DELETE FROM session_events
WHERE type = 'tool.output.delta'
  AND turn_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM session_events AS ended
    WHERE ended.session_id = session_events.session_id
      AND ended.turn_id = session_events.turn_id
      AND ended.type IN ('tool.completed', 'tool.failed', 'tool.cancelled')
      AND json_extract(ended.payload_json, '$.tool_call_id')
        = json_extract(session_events.payload_json, '$.tool_call_id')
  );
