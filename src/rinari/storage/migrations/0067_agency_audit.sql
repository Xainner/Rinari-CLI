-- Rinari Agency, audit of 2026-10-21.
-- soul_id: a saved Soul the owner gives an agent (D-34). NULL: the agent's
-- card is its whole identity, never Rinari's own Soul.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE agency_agents ADD COLUMN soul_id TEXT;

-- Where a rule's result goes besides its run: the office of a team of its
-- agent, or a teammate (deliver_json). NULL: nowhere else, as before.
ALTER TABLE scheduled_tasks ADD COLUMN deliver_json TEXT;

-- Who switched a rule off when it was not the owner: agency_stop for
-- «Detener a todos» (resuming switches it back on) or agent_archived. NULL
-- when it is on, or the owner switched it off himself.
ALTER TABLE scheduled_tasks ADD COLUMN paused_by TEXT;

-- What an errand was doing when «Detener a todos» blocked it (its status and
-- reason), so resuming puts it back as it was.
ALTER TABLE agency_errands ADD COLUMN halted_json TEXT;
