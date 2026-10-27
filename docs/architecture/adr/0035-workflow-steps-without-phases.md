# ADR 0035: Workflow Steps Without Mandatory Phases

- Status: Accepted
- Date: 2026-10-12

Status note (2026-10-26): amended by
[ADR 0038](0038-workflow-containers-for-each-collect-hooks.md) — groups no longer
have a `kind` (loop/parallel); iteration is a separate For each entity
(`registry_workflow_for_each`), and scopes add `for_each_id`.

## Context

Every workflow step used to live inside a phase. Phases carried routing (sort
order, gate and failure targets, branch targets in step config), parallel lanes
(`parallel_group`), loops and boundary checks at once, and the runtime walked a
phase stack. Readiness, canvas, portability and the runtime each re-derived
the graph from that order, and the canvas kept a second edge list. Authoring a
plain sequence required synthetic start/end phases.

## Decision

**Ownership.** A step belongs to its workflow directly:
`registry_workflow_steps.owner_workflow_id`. Loops and parallel lanes are real
groups in `registry_workflow_groups` (`kind` loop or parallel). A step's
nullable `group_id` references a group of the same workflow through the
composite foreign key `(owner_workflow_id, group_id) → (workflow_id, id)`, and
`lane_id` names its lane in a lanes body. The group's `body_jsonb` is the single
source of the body or lane entries. Phases, `parallel_group`, phase gates,
`on_failure`, `phase_kind`, transitions and the unused retry/timeout/SLA/form
policies are gone from the baseline; nothing synthesizes a phase.

**Routing.** The service-owned workflow setting `execution_graph`
(`schema_version: 1`, `entry`, `edges`) is the only routing source. An edge is
`source → outcome → target`; outcomes are `success`, `true`/`false`, `case`
(with a stable `case_id`), `default`, `approved`/`rejected` and `error`.
`group_exit` is the boundary of a group body or lane. `sort_order` and canvas
positions are display only; if/switch configs keep conditions, never targets.
Rework edges are explicit and budgeted (50 per run, optional `rework_limit`
1–50 per edge).

**One validator, one writer.** `validate_workflow_definition` judges the
resulting definition (entry, ports, scopes, reachability, DAG without rework,
placement, Approval/Wait config, boundary contracts, child chains under lanes).
`apply_workflow_changes` is the only writer of steps, groups and the graph for
builder, API and MCP: one change set, one transaction, one revision, checked
against `expected_revision`. An enabled workflow refuses every blocking
structural or environment-dependent readiness issue in the resulting definition,
including pre-existing issues. A repair must resolve all blocking issues in one
atomic change set; incomplete edits require explicit disabling first. Import and
AI-builder drafts create through
`create_workflow_with_definition`. Metadata saves cannot carry `steps`,
`groups` or the graph. Restoring a history entry (`restore_workflow_snapshot`)
replaces the whole definition through the same judge-and-write path as
`apply_workflow_changes`, with the same blocking policy for enabled and disabled
workflows; it is not a second writer.

**Boundary checks.** A phase's expected inputs and required outputs become an
optional `boundary_contract` (`entry`, `exit`, `exit_on`) on the step or real
group where the boundary is, checked with the existing contract rows.

**What a step can read** is the outputs of the steps upstream of it along
non-rework edges in its own scope, plus everything upstream of its group;
parallel sibling lanes are never upstream of each other.

**Message selectors.** The selector dialect is unchanged; its scope is renamed.
A step's latest result is `message.step.<step_id>.<field>` (step ids are unique
per workflow). `message.group.<group_id>.<field>` is the latest result of any
step of that real group — the meaning the phase scope had — so a loop never
accumulates per-item results in the checkpoint. `message.phase.*` is refused at validation
with code `workflow.selector.phase_scope_removed` naming both replacements.

**Portability.** Bundles are `platform.workflow_bundle.v2`: a workflow
definition carries `steps`, `groups` and `execution_graph` (the graph is not a
settings key in a bundle). A v1 phase-based bundle is refused before any write
with `required_schema_version`; there is no converter. Existing installations
recreate their workflows by hand (rollout runbook).

**Runtime.** A run executes the definition pinned before its first dispatch in
the deduplicated `workflow_definition_snapshots` archive (one row per account,
workflow UUID, revision and digest); the job holds the foreign key and
`meta.workflow_definition_ref`, the checkpoint only `{snapshot_id, revision,
digest}` with `runtime_schema_version: 2`. A mismatch with the archive is an
integrity error and a phase-era checkpoint is refused; nothing falls back to
the live registry. One run cursor names the step, its `step_execution_id`
(a new UUID per logical entry; retries keep it) and `attempt`, plus a frame per
real group. The hub accepts only the result of the exact execution and attempt
it dispatched, then decides in a fixed order: contract health of the result,
the host-owned operation retry, the effective error policy (step, then real
group, then `fail`), and finally the graph edge for the outcome. Stop steps end
the run; a rework edge spends the durable budget.

**Recovery approvals.** A recovery pause belongs to one step execution. For a
failed execution (`await_approval`, an exhausted retry, an unmatched
`retry_on`), *approve* continues past the error on the step's success edge,
recorded as an operator-continued error, and *reject* reruns the same
execution with the operator's comment as feedback — the existing
"approve to continue, reject to send back" meaning, without the retired
text-evidence heuristics. Contract errors are never bypassed by approval. An
exhausted rework budget pauses with `approve`/`abort` only: approve raises the
exhausted allowances once and takes the blocked transition; reject is refused.
A pre-step autonomy pause is keyed by the execution it guards.

**Durable waits.** An authored Approval or Wait pauses the run with a LangGraph
interrupt and releases the worker. The hub records the pause at dispatch, the
checkpoint boundary before the node, as `pending_wait` (`wait_id`, kind,
execution identity, definition revision, start; a timer adds `duration_ms`,
`due_at`, `due_epoch_ms`, an approval its `approval_id`), so a re-executed node
never moves a deadline. A Wait duration is resolved once at that point; an
invalid mapped duration is a failure of the Wait step under its effective
error policy, never retried. The job row mirrors the checkpoint pause —
status `awaiting_timer` / `awaiting_approval` / `awaiting_child` and
`jobs.meta.workflow_wait` — and can always be rebuilt from it. Every
continuation uses one durable intent, `jobs.meta.workflow_resume`, published
as queue message `workflow_resume` with `reason` `approval`, `timer` or
`child`; the previous `approval_resume` contract and its in-process intent
list are gone, and an unknown message type is dead-lettered. The control
process claims due timers every second (at most 100), storing the intent and
`resume_pending` in the same statement before publishing; the existing
republisher, revision, lease and finalizer fences apply unchanged. A timer wake
reaches only the wait it names: the checkpoint records the applied wake with
the wake itself, so a redelivery continues from the checkpoint and never
reaches a later pause.

**Durable child invocations.** A serial `invoke_workflow` step (root, serial
loop, a child called serially) no longer blocks a worker on its child. The
child job is created with its parent link (`jobs.meta.invoke_children`, keyed by
`invocation_key` = parent job + step execution) in the same transaction, under
the parent row lock: one child per invocation, none after a parent cancel. The
parent parks `awaiting_child` with a `child` wait; the control process wakes it
once the child job finished (`reason: "child"`), and the step reads the result
through the existing collectors and mappings — a failed, cancelled or stuck
child is a failure of the step under its error policy. A parent cancel records
`cancel_requested_at` on each unfinished child in its own transaction; the
control process applies it (`cancel_applied_at`), recursively, also after the
parent is long terminal. Retention keeps a finished child while its running
parent has not consumed it (`consumed_at`). A child called from a parallel lane,
and every descendant of it (`invoke_lane_placement` in host launch metadata),
keeps the finite path with its timeout and inline execution, and a child whose
definition contains Wait or Approval steps is refused there at run time.

## Consequences

- Readiness, canvas graph, `/dag`, search, knowledge projection, the I/O
  resolver, portability and drafts read `steps`/`groups`/`execution_graph`.
- The admin phase/step CRUD and canvas-topology routes are replaced by
  `POST /api/workflows/{workflow_id}/changes`; I/O routes address
  `/steps/{step_id}/…`.
- The runtime runs on the run cursor and pinned snapshots; step test runs start
  a seeded cursor at the selected step and never follow edges.
- Real groups run on group frames: a loop group walks its sequence or lanes
  body one item at a time, and the parallel executor runs the lanes of a
  parallel group or of a loop item. Authored Approval (`approved`/`rejected`)
  and Wait steps pause durably as described above; serial `invoke_workflow`
  parks `awaiting_child`, and a parent cancel reaches unfinished children
  durably.
- Workflow Automation scopes are workflow, group and step. Its public events
  are `job.completed`, `job.failed`, `job.step_*`, `job.group_*`,
  `job.approval_waiting` (authored, recovery or autonomy; a child's approval is
  also projected to each waiting ancestor with `propagated: true` and its
  `approval_job_id`) and `job.timer_waiting`. `job.phase_*`, `job.gate_waiting`
  and the phase scope are refused, not aliased.
- The builder keeps structural edits local until Apply sends them as one change
  set; an existing step's settings save as one `update_step_config`. Test Run
  is blocked while edits are unapplied or unsaved and sends
  `expected_revision`, so a moved workflow is refused with `409` before a job
  exists. MCP exposes the same writer as `workflow_apply_changes`
  (`admin:all`).
- The job projection names `current_step_id`, `current_group_id`,
  `active_steps` and `workflow_wait`; `current_phase` is gone. Job Details and
  `GET /api/jobs/{job_id}/definition` show the pinned snapshot, and a job
  without one reports `definition_snapshot_unavailable` instead of the live
  definition.
- The extension host contract is `extension_host_v2`: invocation, event and
  grant scopes carry `group_id`/`step_id`/`step_execution_id` instead of
  `phase_id`, and an explicit `group_id: null` means the root. A release built
  for `extension_host_v1` is incompatible, so every external extension and the
  external SDK need a v2 release before an installation switches; see the
  [compatibility matrix](../../operations/workflow-steps-compatibility.md).
- Existing installations move by manual recreation in a separate installation,
  never by conversion: see the
  [rollout runbook](../../operations/workflow-steps-rollout.md).

Related: ADR 0012 (single database source of truth), ADR 0033 (workflow
authoring application boundary).
