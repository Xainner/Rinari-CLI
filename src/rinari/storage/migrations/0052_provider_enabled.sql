-- Turn a provider off without removing it (plan 0.4.0 #97). Off, its models
-- are hidden from every picker, nothing discovers or calls it and its usage
-- is not read. The credential stays. Every saved provider starts on.
ALTER TABLE providers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
