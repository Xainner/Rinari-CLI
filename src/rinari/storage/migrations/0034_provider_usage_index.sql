CREATE INDEX idx_session_events_provider_usage
ON session_events (json_extract(payload_json, '$.provider_id'))
WHERE type = 'ModelInvoked';
