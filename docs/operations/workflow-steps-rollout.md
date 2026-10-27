# Workflow steps rollout runbook

This runbook moves an existing installation whose workflows are phase-based to
a release in which workflows are steps, real groups and one execution graph
([ADR 0035](../architecture/adr/0035-workflow-steps-without-phases.md)). The move
is a **manual recreation in a separate installation**. There is no converter
from `platform.workflow_bundle.v1`, no historical migration and no SQL to run on
an existing database.

What the new release refuses, so nothing old can slip through:

- the change is part of the `db/schema/` baseline, not a migration under
  `db/migrations/`: updating the code does not change an installed database;
- a `platform.workflow_bundle.v1` (phase-based) bundle is refused at import
  before anything is written; a v1 export stays a human-readable reference only;
- a phase-era runtime checkpoint is refused, so a run started on the old
  release cannot continue on the new one;
- reaction rules on `job.phase_started`, `job.phase_completed`,
  `job.phase_failed` or `job.gate_waiting`, and rules of the `phase` scope, are
  refused;
- an extension release built for `extension_host_v1` is incompatible: the host
  sends it nothing. See the
  [compatibility matrix](workflow-steps-compatibility.md).

## Switch gate

Switching a given installation is **blocked** until all four are confirmed and
recorded by the installation's owner:

1. the inventory below is complete;
2. a verified backup and the v1 exports exist;
3. every recreated workflow passed the contract comparison and the test-mode
   smoke run (parity);
4. the owner approved the switch.

This is operational acceptance. Nothing in this runbook authorizes deleting or
recreating anyone's database, or switching a shared or demo installation, on
its own.

## 1. Inventory the old installation

Make the inventory from the installation itself; the list of user workflows
cannot be derived from repository seeds. For each project record:

- workflows and their child dependencies (`invoke_workflow` targets, directly
  and transitively);
- each workflow's public interface: inputs, outputs, artifacts, trigger
  channels;
- automation (reaction) rules and their scopes, and notification routes;
- triggers: schedules, incoming webhook entrypoints, MCP or API callers;
- connections, secrets and the extensions (with versions) the workflows use;
- the owner who confirms parity for each workflow.

Include development and test installations in the owner-confirmed inventory.

Check every extension in the inventory against the
[compatibility matrix](workflow-steps-compatibility.md). An installation whose
active workflows or enabled notification routes depend on an extension without
an `extension_host_v2` release cannot switch yet; do not remove or disable the
integration to get past this check, and do not let approval notifications
silently stop.

## 2. Back up and export

1. Take a verified backup of the old installation with its own tooling (for
   example `./flow-steward backup create`, see
   [backup and recovery](compact-scalable-deployment.md)). Its job history and
   data stay in that installation and its backup; nothing moves them to the new
   one.
2. Export every inventoried workflow as YAML, with its child workflows
   (**Export YAML with child workflows**,
   `/api/workflows/{workflow_id}/export.yaml?include_children=true`). Keep the
   files as the reference for recreation; they will not import into the new
   release.

## 3. Prepare a separate installation

Install the new release from a clean checkout with a **distinct Compose project
identity and its own database** (see
[Initial Setup](../GUIDE.md#step-1--initial-setup)). Do not point it at the old
installation's volumes or database, and never delete the old volumes to get
past an installer guard. Installation Clone copies a source release together
with its data, so it is not a path for this move.

Install the `extension_host_v2` releases of the extensions from the inventory,
then recreate projects, connections and secrets through the normal connection
and secret setup. Secrets are entered again; they are not copied from the old
database.

## 4. Recreate the workflows by hand

Recreate child workflows before the workflows that call them. For each
workflow, using its v1 export as the reference:

| Phase-era element | Recreate as |
|---|---|
| Ordinary phase | Its steps at the root, connected by `success` edges. |
| Phase with `loop` | A For each (`over`, `item_name`) with a sequence body, ending at its `for_each_exit`; collected arrays are `collect` steps into variables created before it. |
| `parallel_group` lanes | A Group with a lanes body (one lane per former parallel group), or a For each with a lanes body. |
| Human gate | An `approval` step placed before the work it guards, with `approved` and `rejected` edges; a reject that sent work back is a rework edge. |
| Conditional or auto-approve gate | An `if` or `switch` step routing to the next step, an Approval step or a Stop step. |
| Branch target inside an `if`/`switch` config | An edge from the named outcome (`true`/`false`, `case` with `case_id`, `default`). |
| `on_failure`, retry previous, skip, escalate | The step's or group's `error_policy` (`retry`, `continue`, `await_approval`, or `route_to_step` with an `error` edge). |
| `expected_inputs` / `required_outputs` | A `boundary_contract` (`entry` / `exit`, with `exit_on` where a step has both inner and leaving outcomes) on every step where the former phase is entered or left. |
| `message.phase.<id>.<field>` selectors | `message.step.<step_id>.<field>`, or `message.group.<group_id>.<field>` for a former parallel phase. |
| Reaction on a phase event or `job.gate_waiting` | A `job.group_*` rule for a former parallel phase; a `job.step_*` or `job.approval_waiting` rule for an ordinary former phase or a former loop phase. |

Where the old boundaries are ambiguous, decide them explicitly in the new
definition; do not let a tool guess. Keep the new workflows **disabled** until
step 6. Recreate their automation rules and notification routes.

## 5. Compare contracts and smoke-test

For each recreated workflow:

1. Compare its public interface (inputs, outputs, artifacts, trigger channels)
   with the inventory; a calling workflow or external caller must not notice the
   move.
2. Validate it: no definition or readiness issues.
3. Run **Test Run** in test mode with representative inputs, avoiding repeated
   production effects. Test mode does not guarantee that every external mutation
   is suppressed, so check each external action first. Cover each branch,
   Approval and Wait step, and each child workflow.
4. Have the workflow's owner confirm parity.

## 6. Switch

1. Finish the old installation's active runs — pending, in progress, awaiting
   approval and resume pending — or cancel them explicitly by an operator's
   decision. Nothing cancels them automatically, and none of them can continue
   on the new release.
2. Stop the old installation's triggers and deliveries: pause its schedules,
   disable the workflows its webhook entrypoints and external callers launch,
   and disable its notification routes.
3. Only then enable the new workflows, schedules, webhook entrypoints and
   routes. **Never have the same schedule or webhook active in both
   installations at once.** Point external callers and webhook senders at the
   new installation.
4. Watch the first production runs in Job Details: each shows the definition
   revision it executed.

Keep the old installation (stopped) and its backup as the record of its history
until the owner no longer needs them.

## Rollback

Before step 6.3 nothing has changed for callers: re-enable the old triggers. After
it, stop the new installation's triggers first, then re-enable the old ones —
again never both at once. Runs already finished on the new installation stay
there; the old installation cannot resume them.
