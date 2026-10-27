# ADR 0038: Run-Once Groups, a Standalone For Each, Collect and Hooks

- Status: Accepted
- Date: 2026-10-15
- Amends: [ADR 0035](0035-workflow-steps-without-phases.md) (groups, loops, scope)

## Context

ADR 0035 made loops and parallel lanes real groups: one `registry_workflow_groups`
row with `kind` `loop` or `parallel`. A second, unrelated iteration lived inside
a single step (`step_kind=for_each` with inline `override.for_each.steps[]`), with
its own executor, collect rules, `skip_when`/`break_when` and `continue_on_error`.
Two iteration models, a group that is "secretly a loop", no explicit
Break/Continue, and no way for an extension author to leave a customisable call
in a protected workflow.

The product decisions were fixed in the 2026-10-15 developer task for groups,
For each, Collect and hooks (a planning note not kept in the repository);
this ADR records the architecture that implements them. It does not reopen them.

## Decision

**Two containers.** A *Group* runs its body once. Its `body.kind` is `sequence`
or `lanes`; there is no `kind` discriminator and no loop/source/item/index field.
A *For each* is a separate definition entity (`for_each[]` beside `steps[]` and
`groups[]`) and a separate table `registry_workflow_for_each`. It has
`for_each_id`, an optional parent `group_id`, `over`, `item_name`, `index_name`,
the same `body` shape, `error_policy` and `boundary_contract`. Iterations are
sequential; a lanes body means parallel lanes *within one item*.

**Ownership.** A step has `group_id` and `for_each_id`, both nullable, at most
one set (DB CHECK and validator). Both null means root. A step of a For each never
carries its ancestor Group. Scope is derived, never stored:
`root`, `group(id, lane?)`, `for_each(id, lane?)`. Node refs are
`{kind: step|group|for_each, id}`; business ids are unique per workflow. Exits are
`group_exit` and `for_each_exit` (`{kind, <owner>_id, lane_id}`): the end of the
body or lane — for For each, the end of the current item.

**Composition (first version).** Root holds steps, Groups and For each. A sequence
Group may hold steps and For each. A parallel Group lane, a For each body and a
For each lane hold no containers. Break, Continue and Collect are valid only in a
sequence For each body. One composition matrix in the definition rules serves
create, kind change, move, body change, import, every API surface and run start.

**Iteration.** `over` is resolved once on entry. A list is iterated as is;
null/missing is empty; a scalar or object is one item; a dictionary is not
expanded; a dataset cursor descriptor is refused (`for_each.source_cursor_unsupported`).
The single operator limit is `FS_DATASET_QUERY_MAX_RUNTIME_ROWS` (default 1000,
positive integer, invalid values refused) read through one config getter. For each
takes the first `min(count, N)` items and records `source_count`,
`selected_count`, `processed_count`, `applied_limit`, `truncated`. The selected
items, the limit and a digest are fixed in the `ForEachFrame` of the checkpointed
run cursor; changing the source variable or N does not extend a started pass.
`item_name`/`index_name` are published through `workflow_data`. Each item gives
each body step a new `step_execution_id`; retries keep it. Finite items do not
spend the runaway or rework budgets.

**Run cursor.** One container stack: `GroupFrame` (no iteration fields) and
`ForEachFrame` (execution id, item index, selected items, counts, limit, digest).
At most two frames (Group → For each). One hub/body runner serves both.

**Controls.** `break` and `continue` are terminal control nodes of a For each body
with no outgoing edges. Continue ends the item; Break ends the For each normally
(exit contract still checked). Stop Success / Stop Fail keep their names and end
the current workflow; they are never caught by a container. Break/Continue never
cross an Invoke boundary.

**Errors.** The failed step's policy is the first declared one in the order
step → immediate For each → enclosing Group → `fail`, taken whole from one source
(recorded as a NodeRef). An inherited `route_to_step` fails the container that
declared it, unwinds that frame and every inner frame, and follows that
container's error edge in its parent scope. An unhandled failure bubbles outward
container by container; a container's `continue`/`retry`/`await_approval` is the
default for its children's failures only and is never re-applied to the whole
container. Boundary failures (entry contract, source, item publication, exit
contract) fail the container and bubble the same way; exit-contract recovery
keeps the existing approve/reject meaning. The order contract health → operation
retry/recovery → authored policy is unchanged.

**Collect.** `step_kind=collect` (`target`, `source`, `mode=append|set_key`,
`key`) appends one element to an existing `array` target or sets one literal
dictionary key on an existing `object` target, then writes the whole target
through the ordinary state assignment. No hidden creation, reset, flatten or
unique. The write and the logical completion of the step share the existing
checkpoint boundary, so a redelivery of the same execution never appends twice.
The inline For each executor and its collect/skip/break rules are removed.

**Hooks.** A hook is an ordinary `invoke_workflow` step whose config carries
`hook` instead of `workflow_id`. A client binding per
`(account, project, source_install_id, local_workflow_id, hook_id)` names an
ordinary reusable child and its mappings; bindings have active pointers and
immutable revisions. One resolver (`resolve_invocation_target`) gives the
effective target for direct Invoke and hooks, and every child-chain walker uses
it. The owner job pins its definition and its binding revisions (or explicit
absence) at submission; the child definition is pinned at the child's own
submission. No binding means passthrough/defaults and Success without a child.
The output is published atomically at `state.hooks.<hook_id>.output`. Success and
On error are the only outcomes.

**A hook is a place for steps (R2-11, 2026-10-22).** In the builder a hook is a
frame on the canvas where a project adds steps; the UI never names, chooses,
connects or disconnects a workflow behind it. Its steps are stored in a hidden
per-project workflow that the first added step creates through
`create-and-bind`: empty, enabled and callable, holding only the hidden end
step `hook_end` (`stop_success`, the entry), with every hook input as an input
and every mutable or output-only hook field as an optional `variables` output
mapped back to the field of the same name. It is marked
`settings.hook_storage = {author_workflow_id, hook_id, project_id}` (a
service-owned key) and `visible_in_catalog = false`; the marker, not the step
id, identifies it, and every list a person browses hides it: the workflow
registry list and search, the Invoke target picker (`meta/callable-workflows`),
the project's workflow bindings (and with them the MCP/CLI/API `workflow_list`
and the project workflow catalog) and the workflow choices. Saving a project's
workflow selection keeps its link to the project. Direct reads by id still
work. The canvas never draws `hook_end`:
a member that leads there is drawn as reaching the end of the frame, the first
node added to an empty hook becomes its entry and leads to `hook_end`, and when
the last node goes `hook_end` is the entry again. "Clear the hook" removes
every other node as one unapplied change (Undo/Discard bring them back). A
storage workflow that holds only `hook_end` runs nothing: the hook passes its
inputs through exactly like a hook without a binding. A storage workflow is
complete without a "real execution" step (a hook made only of Set variable
steps changes its results), and an optional callable output no step produces
is a readiness warning, not an error. Results are read by name from the hook's
state at its end: a mapped result its steps did not set keeps the hook input
of that name (an output-only field its declared default) instead of failing
with `workflow.hook.mapped_output_missing`; a value of the wrong type still
fails the hook. To call a reusable workflow inside a hook, add an Invoke
workflow step there. Binding a hook to any existing callable workflow through
the operator API, CLI, MCP and import stays available for automation.

Bindings live in `registry_workflow_hook_bindings` (one row per scope key, an
optimistic `revision` and the active revision) and
`registry_workflow_hook_binding_revisions` (never updated; a cleared binding is
a revision without a target). A job records `jobs.meta.hook_binding_pins`
(`hook_id` → revision id or `null`) in the transaction that pins its
definition, holding the binding rows `FOR SHARE` so a concurrent save waits;
revisions are kept, so the pin needs no retention traversal. The active
revisions are also the hook half of the reverse dependency model: an owner save
rewrites `workflow_runtime_dependencies` for the owner workflow, which would
erase project-scoped rows, so where-used reads direct Invokes from that table
and hook targets from the active revisions. Authoring readiness has no project,
so it walks direct Invokes only; a hook's chain (recursion, depth, lane waits)
is checked by the binding save and again at each pin and child submission.

**Binding portability.** A project-scoped export (operator `export_workflow`:
CLI, MCP, `GET /api/projects/{p}/workflows/{w}/export`) adds an optional
top-level `hook_bindings` list to the same `platform.workflow_bundle.v2` bundle:
per bound hook its `hook_id`, `owner_workflow_id`, the `hook_contract_version`
and `hook_semantic_digest` it was saved against, `target_workflow_id` and both
mappings, never account or project ids. The bound child is an ordinary
`child_workflows` dependency and travels with `include_children` under its own
export policy. The admin YAML export has no project and writes no bindings.
Import checks each binding in preflight (owner and hook present, same contract,
target carried or present, mappings fit), then saves it through the binding
service inside the import transaction with remapped owner and target ids; a
refusal rolls the whole import back. A draft save refuses a bundle with
bindings rather than dropping them.

**Protected author workflows.** Reading the structure, mutating it and executing
one of its steps in isolation are separate policy decisions. A protected author
graph is readable (redacted) and never editable, clonable, fully exportable or
step-test-runnable.

**Execution scope.** The extension host contract keeps the token
`extension_host_v2` as the single final contract. Every action/query/event scope
carries all four keys `group_id`, `for_each_id`, `step_id`, `step_execution_id`
(string or null). The handshake confirms `result.execution_scope_fields` exactly;
grants sign `for_each_id` too, and an explicit null is a binding, not a wildcard.

**Versions.** The technical identifiers stay: execution graph
`schema_version: 1`, definition snapshot `schema_version: 1`, checkpoint
`runtime_schema_version: 2`, bundle `platform.workflow_bundle.v2`, host
`extension_host_v2`. Only their final structure is accepted. Group `kind`,
`loop`, inline `for_each` and three-field scopes are refused before any write,
apply or effect; nothing converts or reads them.

## Consequences

- `registry_workflow_groups` loses `kind` and `loop`; `registry_workflow_steps`
  gains `for_each_id`; `registry_workflow_for_each` is new. Canonical schema only,
  no migrations (pre-1.0).
- Existing pre-release definitions, bundles, checkpoints and extension packages
  with the old shapes stop working and are recreated (runbook).
- Builder, API, MCP, CLI, portability, Convert Group to Child, Job Details and
  the SDK speak the same final model.
