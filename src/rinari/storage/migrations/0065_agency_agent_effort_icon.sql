-- Rinari Agency: an agent's reasoning effort (its turns, its posts and its
-- rules use it when the turn does not choose one) and its icon (an id the app
-- maps to an avatar, opaque to the Engine). Both NULL: the model's default and
-- an avatar chosen by the agent's id.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE agency_agents ADD COLUMN reasoning_effort TEXT;

ALTER TABLE agency_agents ADD COLUMN icon TEXT;
