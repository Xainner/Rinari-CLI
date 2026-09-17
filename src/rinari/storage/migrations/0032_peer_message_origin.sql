-- Procedencia estructurada de mensajes (peer / automation / user). Nullable:
-- las filas anteriores conservan el comportamiento legacy sin origen.
ALTER TABLE session_messages ADD COLUMN origin_json TEXT;
