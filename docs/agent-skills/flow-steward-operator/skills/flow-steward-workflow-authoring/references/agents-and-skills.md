# Agents and Skills

Agents and skills are account registry objects. Every tool here needs an
administrator user, and a token needs `admin:all`, reads included. The schema
tools return the current bodies; read them before the first write in a
conversation instead of relying on remembered fields.

## Skills

- `skill_authoring_schema`: `create.json_schema` is the body of
  `skill_create`, `patch.json_schema` the `changes` of `skill_update`, and
  `related.inputs`/`related.outputs` the item schema of the contract lists.
- `skill_create(skill)` creates exactly `skill['skill_id']`, saved disabled.
  Omitted fields take schema defaults. The result has `status`, `skill_id` and
  the readiness `validation`.
- `skill_update(skill_id, changes)`: every field sent replaces the stored
  value; null never clears one; an empty `changes` answers `no_change`.
- `skill_inputs_set`/`skill_outputs_set` replace the whole list (items: name,
  type, description, required). Invalid items are refused and nothing changes.
  Agents using the skill pick up the new contract, so check
  `agent_list`/`agent_get` for its users before changing it.
- `skill_validate` checks readiness without changing anything;
  `skill_set_enabled(skill_id, enabled)` sets exactly that state. Disabling is
  refused while enabled workflows depend on an agent using the skill.

A skill is instructions for an agent. It does not reach any external system,
and it holds no credentials.

## Agents

- `agent_authoring_schema`: `create.json_schema` is the body of `agent_create`,
  `patch.json_schema` the `changes` of `agent_update`, `patch.semantics` how a
  patch merges, `related` the readiness, enable and assignment operations.
- `agent_create(agent)` creates exactly `agent['agent_id']`, saved disabled.
  `skills` may list `{skill_id, skill_prompt}` assignments.
- `agent_update(agent_id, changes)`: only the fields sent change; null never
  clears; `config` and `prompts` merge shallowly; an empty
  `config.provider`/`config.model` inherits the project route. Skills and
  tools are refused here.
- `agent_skill_assign(agent_id, skill_id, skill_prompt)` adds a skill with
  agent-specific instructions; an already assigned skill is a 409, an unknown
  one a 404, one the agent's contract cannot use is refused with its issues.
  `agent_skill_unassign` removes it; `agent_skill_set_instructions` replaces
  the instructions of one assignment.
- `agent_validate` checks readiness; `agent_set_enabled(agent_id, enabled)`
  sets exactly that state. Enabling needs a valid readiness result; disabling
  is refused while enabled workflows run the agent.
- `model_options` gives the selectable `provider_id`/`model_id` values.

Not available through MCP: reordering skills, replacing all relationships at
once, an agent's tools, full replacement, clone, history, restore and delete.
Say so and point the user to the registry screens when they need one.

## Read before you write: the last write wins

Agents and skills have no revision check. Another person or agent may change
the same object between your read and your write, and your write replaces what
it names.

- Read with `agent_get`/`skill_get` right before each change, and send only
  the fields you mean to change.
- If the read shows changes you did not expect, show them to the user before
  writing.
- After an uncertain result, read again before any retry.

## Conflicts

A refused write is a tool error whose `error.details` carries the route's
payload unchanged. Nothing was saved and nothing is renamed or retried for you.

### `registry_id_conflict`

The id is taken. The taken object may be invisible to this user (in the Trash
or another account), so the payload may show no match. `suggested_id` is a free
id at the time of the answer; nothing reserves it.

- After an uncertain create of that same id: read it with `agent_get` or
  `skill_get`. If you can read it and it matches what you sent, it is probably
  your own object: say that its origin is unconfirmed and continue with it. If
  you cannot read it, or it differs, it is not yours.
- Otherwise tell the user the id is taken and offer the suggested id or one
  they choose. Create under a new id only with their answer.

### `registry_name_conflict`

The display name is used by objects this user can see. The payload has
`matches` (each with `id`, `display_name`, `enabled`, `updated_at`, `origin`,
and `used_by` with the workflows or agents that use it), truncation flags when
there are more, and `suggested_display_name`, a hint nothing reserves.

1. Read each match (`agent_get`/`skill_get`).
2. If a match is the object you just tried to create after an uncertain
   result (same id, same content), say that its origin is unconfirmed and
   continue with it. Do not create another one.
3. Otherwise show the user every match: id, enabled state, last change,
   origin, where it is used (say when the list is truncated) and how it differs
   from what you meant to create. Ask whether to reuse or change the existing
   object, or which new name to use. Wait for the answer.

Changing an existing object that other workflows use changes them too; say
which ones before you write.

### Other refusals

- `Contract validation failed` (400) carries `issues`: fix them and try again.
- A dependency conflict (409) names the enabled workflows that depend on the
  object; disabling waits until they no longer do, which is the user's call.
