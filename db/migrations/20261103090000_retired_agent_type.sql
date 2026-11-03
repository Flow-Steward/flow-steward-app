-- The current agent registry has no agent type relationship. Older databases may
-- retain a NOT NULL foreign key with a random default, causing new agent inserts
-- to fail. Remove only that retired column and retain unrelated objects.
-- transaction: true
ALTER TABLE ONLY public.registry_agents DROP COLUMN IF EXISTS agent_type_id;
