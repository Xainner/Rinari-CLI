-- Plan permisos v4 P-5: a scheduled task runs in its own permission mode.
-- NULL means the project's default mode (or Proyecto/workspace without one).
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE scheduled_tasks ADD COLUMN permission_profile TEXT;
