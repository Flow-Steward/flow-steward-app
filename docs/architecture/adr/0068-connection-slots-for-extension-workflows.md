# ADR 0068: Connection slots for extension workflows

- Status: Accepted
- Date: 2026-11-14
- Amends: [ADR 0065](0065-composite-extension-actions.md)
- Builds on: [ADR 0038](0038-workflow-containers-for-each-collect-hooks.md),
  [ADR 0056](0056-pre-1.0-released-database-upgrades.md),
  [ADR 0067](0067-extension-page-queries-with-connection.md)

## Context

An extension can ship a workflow read-only: people see its steps, add their own
only in its hooks (ADR 0038), and an update replaces the author's steps while
the hook steps stay. A connector step of such a workflow cannot get the
project's connection:

- the runtime refuses an operation that needs a connection when the step names
  none (`connection_resolution_failed: explicit connection binding is
  required`);
- a bundle never carries a connection id, and the project cannot edit the
  steps of a read-only workflow;
- nothing chooses a project connection for the step.

Extension authors therefore shipped project-owned copies, which every update
misses. Composite actions (ADR 0065) already solved the same problem for a
hidden workflow: a connector step binds `{slot: <name>}`, and the host fills the
slot from the action context of the job (`jobs.meta.composite_action`). What is
missing is who fills the slots when the workflow is started directly: from the
UI, a webhook, a schedule or an Invoke.

## Decision

### Declaration

An extension workflow declares the connections its connector steps read in its
settings, beside its Interface:

```yaml
settings:
  connection_slots:
    - slot: store                      # ^[a-z][a-z0-9_]{0,62}$, unique
      extension_id: acme.store         # this extension or a required dependency
      connection_type_ids: [api_token] # empty = any type of that extension
      label: Store account             # what people see; optional, <= 120 chars
```

The shape is an action implementation's slot (ADR 0065) plus `label`. It is a
top-level settings key, not part of `settings.callable`, because a workflow
that is not callable has no `settings.callable`.

A connector step reads a slot as `connection_binding: {slot: <name>}`; an action
Invoke passes it on as `connections: {<their slot>: {slot: <name>}}`. The
release is refused (`workflow.connection_slots.*`) when the declaration is
malformed, names an extension the bundle may not call, sits on an action
implementation (which keeps its slots in `action_implementation`), when a step
or an action Invoke reads an undeclared slot, or when a step carries a fixed
connection id next to its slot. A bundle that declares slots requires
`runtime.compatibility.platform_min: 1.8.0`; the host contract is now 1.8.0.

Only a workflow an extension install projected has its slots filled. An
account's own workflow (the author's workflow in a Development environment)
keeps the ADR 0065 rule: a step names the slot it will read and a connection to
test with.

### The project's choice

A project picks one connection per slot. The choice is stored in
`workflow_connection_slot_choices`, keyed by account, project, source install,
local workflow id and slot, never by release, so it survives updates. The
connection reference is a foreign key on `project_connections (connection_id,
project_id, account_id)` with `ON DELETE CASCADE`: the connection belongs to the
same project, and deleting it, the project or the account removes the choice.

One rule decides what fills a slot, and every reader uses it:

1. the stored choice, while that connection is enabled, healthy, of the slot's
   extension and of an accepted type;
2. otherwise, when the project has exactly one usable connection for the slot,
   that one ("chosen automatically", not written);
3. otherwise nothing: the workflow is not ready in that project, with a plain
   reason (create one, or choose among N).

A stored choice that stopped fitting is never replaced silently; the person
chooses again.

API, admin-only like hook bindings, for a workflow linked to the project:

- `GET /api/projects/{project_id}/workflows/{workflow_id}/connection-slots`
  returns `{workflow_id, project_id, ready, slots: [{slot, label,
  extension_id, connection_type_ids, connection_id, choice: chosen|automatic|
  none, ready, reason, options: [{connection_id, display_name,
  connection_type, extension_install_id}]}]}`.
- `PUT …/connection-slots/{slot}` with `{connection_id, release_id?}` stores a
  usable connection, or clears the choice when `connection_id` is empty.
  `release_id` validates the slot against a pending release, so a project can
  choose before an update.

The workflow's Overview shows "This workflow needs: <label>" with a selector per
slot, the state (chosen, chosen automatically, not ready) and the reason.

### Pin at submission

Every new job passes `enqueue_job`. For a projected extension workflow that
declares slots, the host resolves each slot with the rule above and writes
`jobs.meta.connection_slots = {connections: {slot: connection_id}, installs:
{extension_id: install_id}, workflow_id}`, the same `connections`/`installs`
shape an action job carries. A value a caller puts under that key is dropped
first. A slot without a connection refuses the submission with 409
`workflow.connection_slots.not_ready` and the reason of each slot, before the
job exists: a run from the UI, a webhook, a schedule, a provider trigger, a
restart and an Invoke child alike. A run keeps the connection it started with.

An action child keeps the action context its Invoke wrote (ADR 0065) and gets
no pin. A child extension workflow started by an Invoke in the same project gets
the parent run's connection for a slot of the same name when that connection
fits the child's slot; otherwise the project's choice for the child.

### Runtime

Slot resolution is unchanged (`resolve_connector_action_binding`). The context
it reads is the action context of an action child, or else the pinned slots.
A nested action Invoke inside a slot workflow passes the workflow's slots on,
as inside an implementation. Readiness no longer reports
`workflow.connection_required` for a step that reads a slot; it reports
`workflow.connection_slots.undeclared` when the workflow does not declare that
slot and the step names no test connection.

### Update

Before a release's workflows are projected, every slot the release adds, or
whose extension or connection types it changes, is resolved in every project
that links the workflow. A slot that would stay empty blocks the activation
with 409 and `connection_slots: [{code:
workflow.connection_slots.release_needs_choice, workflow_id, project_id, slot,
release_id, message}]`, as a changed hook does (ADR 0038). A label change never
blocks. When the release activates, the choices of slots it no longer declares
are removed.

### Cleanup and portability

Uninstall deletes the install's choices; connection, project and account
deletion cascade. Export and import never carry connection ids: the choice
lives outside the definition, and the declaration holds only slot names, types
and labels.

### Database

Migration `20261114090000_workflow_connection_slot_choices.sql` creates the
table and its two indexes with `IF NOT EXISTS`; the same block ends
`db/schema/001_schema_part_12.sql` (ADR 0012 amendment, ADR 0056). No existing
data moves.

## Consequences

- A read-only extension workflow can call a connector that needs a project
  connection; no project-owned copy is needed for it, and every other extension
  that ships such a workflow can use the same declaration.
- There is no per-run choice of connection. A project that needs two
  connections links two workflows, or calls the workflow's action from its own
  workflow through an action Invoke, which passes slots.
- The automatic choice changes when a project adds a second usable connection:
  the workflow becomes not ready until the person chooses. Choosing explicitly
  keeps it stable.
- Projects linked before an update must have a usable connection for a new
  slot before the update activates; with exactly one, nothing is asked.
- CLI and MCP commands for the choice are not part of this release; the API and
  the Overview are.
