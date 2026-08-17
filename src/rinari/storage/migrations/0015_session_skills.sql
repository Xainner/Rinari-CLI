-- 0015: durable active-skill state for resume reconciliation (phase 4).
-- Stores the (name, version) pairs of skills a session had active so that
-- `rinari resume` can warn when a skill was uninstalled or its version
-- changed on disk while the session was paused. The skill runtime itself
-- lands in phase 6, but the state and the reconciliation exist now so the
-- contract is testable without dead code.
ALTER TABLE sessions ADD COLUMN active_skills_json TEXT;