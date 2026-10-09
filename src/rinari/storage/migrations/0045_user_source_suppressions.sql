-- Deleting a conversation wrote a source tombstone for every message, while
-- only the owner's messages are memory sources (one install held 5256 rows).
-- Drop the tombstones of messages that are not the owner's. For a
-- conversation that is already gone the message roles are gone with it, and
-- its 'deleted' control row is the tombstone every extraction path checks
-- first, so its per-message rows go too. NOTE: no semicolons in comments.
DELETE FROM memory_source_suppressions
WHERE EXISTS (
        SELECT 1 FROM session_messages m
        WHERE m.id = memory_source_suppressions.message_id
          AND m.session_id = memory_source_suppressions.session_id
          AND m.role != 'user'
    )
   OR (
        NOT EXISTS (
            SELECT 1 FROM sessions s WHERE s.id = memory_source_suppressions.session_id
        )
        AND EXISTS (
            SELECT 1 FROM memory_conversation_controls c
            WHERE c.session_id = memory_source_suppressions.session_id
              AND c.mode = 'deleted'
        )
    )
