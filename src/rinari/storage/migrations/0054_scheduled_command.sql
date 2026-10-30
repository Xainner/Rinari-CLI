-- Plan 0.4.0 #50: a scheduled task can run a command or a script without the model.
-- command_json holds the command, shell, folder, limit and what to do with the result.
-- NOTE: no semicolons in comments (the runner splits on them).
ALTER TABLE scheduled_tasks ADD COLUMN command_json TEXT;
