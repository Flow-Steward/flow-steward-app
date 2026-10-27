---
name: flow-steward-workflow-authoring
description: Use when a Flow Steward user wants to find, adapt, compose, or change workflows, inspect runs or pending approvals, or request operational reports through an existing MCP connection.
---

# Flow Steward Workflows and Operations

Work through the user's already configured authenticated Flow Steward MCP.
A platform source checkout is not required. Use `tools/list` for live arguments
and token-filtered availability; the server owns permissions, contracts and
runtime behavior. Version-matched documentation is optional supporting context;
missing repository files must not block ordinary MCP use. If the connection or
required tool is unavailable, report the gap without installing a server,
changing credentials or expanding scope.

Establish the requested project/workflow and outcome. Resolve names through
accessible `project_list`/`workflow_list`; retain exact returned IDs when a tool
needs `project_id` or `workflow_id`. Omitted filters cover the caller's existing
scope, so do not silently omit a requested filter or broaden a failed query.
Read failures remain failures, never an empty successful result.

## Inspect runs, errors and human gates

- Use `job_list(project_id=..., workflow_id=..., group_by="attention")` for a
  bounded triage snapshot. Grouped attention ignores `page`/`per_page`; it is not
  a complete paginated inventory. State that limit when reporting counts.
- For a complete list or time-filtered investigation, use ungrouped `job_list`
  with supported exact project/workflow/status filters and `page`/`per_page`.
  Follow returned `total_pages`; classify only actual returned statuses and
  acknowledgment fields. Report partial pages or a changing inventory.
  `job_list` has no date arguments: filter returned timestamps locally for the
  requested interval and identify which timestamp (queued, started or finished)
  defines it. Do not claim a complete date result from one page or the snapshot.
- Use `job_approvals_pending` to find accessible pending gates, then constrain
  the returned results to the requested project/workflow. Read `job_approval`
  for a known job and `job_approvals` for its history. Do not infer an actionable
  approval solely from a status label. `awaiting_timer` (a Wait step) and
  `awaiting_child` (a parent waiting for a child workflow) are not approvals: a
  parent's pending decision lives on the child job it links to, and a decision
  must use that child job's `thread_id` and `approval_id`. Read
  `allowed_actions`: an exhausted rework budget offers `approve` and `abort`
  only.
- Inspect known jobs with `job_status`, `job_result`, `job_errors`,
  `job_incident`, `job_trace`, `job_logs` or `job_events` as needed. Preserve
  pagination/cursor limits and distinguish current status from a final result.
  Keep workflow runtime messages and approval-thread context with their jobs.

Present job IDs, observed status, errors or gate context and the next useful
action. Inspection does not authorize acknowledgment, restart, cancellation or
approval. For a requested approval decision, obtain the human's actual decision
and use returned `thread_id`/`approval_id` with `job_approval_decide` and a stable
`idempotency_key`; never approve automatically because launch was authorized.

## Request an operational report

Use `report_generate` for `activity_daily`, `project_summary` or `project_stats`
with `params.project_id`, exact `params.workflow_id` and either `params.range`
or both ISO `params.start_date`/`params.end_date`. These are operational activity
reports; do not infer complete automation-spend or financial reporting from
incidental cost fields. For one known job, `job_summary` requires `params.job_id`.
Use `report_get(report_id)` only for an accessible persisted report.

Clarify or state the period and timezone. `range="7d"` is a rolling 168-hour UTC
window, not seven local calendar days. Explicit dates are inclusive UTC calendar
days: an end date includes that day through the next midnight UTC. The default
range is `7d`; set the requested period explicitly. For local "today", determine
the user's timezone and actual date, convert its start/end to UTC, and use
complete ungrouped job pages with local timestamp filtering if exact local-day
results are needed. UTC-day aggregates cannot establish an exact local-day
report; label them UTC or report that limitation instead of relabeling them.

`persist=false` avoids storing the report but still requires `reports.write`
(or an accepted broader scope). Never present it as a read-only authorization
bypass. Set persistence to match the user's request; report permission denial
without substituting credentials. Return the actual window, filters, coverage,
report ID when persisted, and limitations alongside the observed results.

## Reuse or compose a workflow

1. Establish the behavior, required inputs and outputs. Call
   `workflow_examples_search(project_name, query)` before creating a workflow.
   Inspect promising references with `capability_describe`. Respect protected
   exports; a protected reference is not an editable definition.
2. If one meets the need, return its identity, inputs and observed readiness.
   Use project-scoped `workflow_contract` and `workflow_validate` when needed.
   Description, example and graph results do not prove runtime readiness.
   Launch through the existing `workflow_trigger` only when execution is
   requested; inspect its live input/channel contract and track the returned
   job. Reuse does not require saving a duplicate.
3. Otherwise use `capability_search` and `capability_describe` for missing
   components and live operation contracts. Select `operation_id` for
   multi-operation tools. Check permitted connections, readiness and setup.
   Report no matches and knowledge freshness. These tools search accessible
   local knowledge, not marketplace workflows or extensions.
4. Get `workflow_authoring_schema`, including relevant `step_kind` guidance.
   Start from `minimal_bundle_yaml` or a permitted export. Author exactly one
   workflow in `platform.workflow_bundle.v2` with an explicit stable
   `workflow_id`: its `steps` (a step in a loop or parallel group names its
   `group_id`, and in a lanes body its `lane_id`), its `groups`, and one
   `execution_graph` with an `entry` and edges `source → outcome → target`.
   There are no phases; route every outcome (`success`, `true`/`false`, a
   switch `case` by `case_id` plus `default`, `approved`/`rejected`) to exactly
   one target and end root paths at `stop_success` or `stop_fail`. A phase-based
   v1 bundle or export is refused, not converted. Use the returned persisted
   field locations and schemas for configuration/mappings; adaptation creates a
   new draft.
5. Call `workflow_draft_validate(project_name, bundle_yaml, resolutions)`.
   Repair actual structural, reference, mapping and contract issues, keeping
   the ID. Only `use_existing`/`map_existing` resolutions are supported.
   Missing dependencies need truthful readiness reporting, not placeholders
   or installation. Inspect `can_save`, issues and performed/unperformed
   checks. Validation does not execute the workflow.
6. When draft creation is requested and validation permits it, call
   `workflow_draft_save` with that same bundle/resolutions. An admin user's
   scoped PAT needs `admin:all`; machine tokens remain operators even with
   that scope. `workflows.write` permits launch, not draft creation.
   Read-only project-name resolution also needs `projects.read`. Report
   denial without changing credentials or treating it as successful creation.

## Change an existing workflow

Draft save never edits an existing workflow. When the user asks to change one
and the token holds `admin:all`, use `workflow_apply_changes(workflow_id,
expected_revision, operations)`: one atomic set of `create_step`,
`create_group`, `update_step_config`, `change_step_kind`,
`update_group_config`, `set_entry`, `connect`, `disconnect`,
`insert_step_between`, `move_step`, `delete_step` and `delete_group`
operations, using stable ids you assign so later operations can name new
nodes. The set becomes one new revision or nothing is saved. Pass
`dry_run=true` first to see the resulting issues, and which of them would
refuse the set, without saving anything.

- Read the workflow first with `workflow_definition(workflow_id)`: its steps,
  groups, execution graph and `revision`. Pass that `revision` (or the one a
  previous `workflow_apply_changes` returned) as `expected_revision`. A
  `409 workflow_revision_conflict` means the workflow changed; it names the
  current `revision`. Read the definition again and rebuild the set; never
  retry the stale set blindly or overwrite.
- An enabled workflow refuses a result with blocking issues and keeps running
  its current revision; report the returned issues. Do not disable the workflow
  to get a change through unless the user asked for that.
- Every agent, child workflow, connection and extension install an operation
  names must be usable by the workflow's account;
  `workflow.change.reference_unavailable` does not say whether a foreign id
  exists.
- A moved step loses its edges, and a deleted step leaves no bypass: reconnect
  explicitly in the same set. Report the new revision and remaining issues.

## Export or import a workflow

`workflow_export(project_name, workflow_id, include_children)` returns the
workflow as `platform.workflow_bundle.v2` YAML (`bundle_yaml`); set
`include_children=true` when it calls other workflows, so they travel in the
same file. `workflow_import(project_name, bundle_yaml, target_workflow_id,
resolutions)` creates disabled drafts and needs what draft save needs
(`admin:all`). Exports leave secret values out; still read an export before
sharing it outside the installation. To publish a workflow to the public
catalog, use the `flow-steward-publish-workflow` skill.

## Recover from an uncertain save

For automatic recovery after a save timeout, keep the same ID and unchanged
bundle/resolutions. A `workflow_id_conflict` by itself does not authorize a
rename or overwrite. If the user explicitly requests an additional draft under
a new ID, honor that request: select the new ID, change the bundle through the
known schema, revalidate the changed bundle and save only that validated version.
That one request does not authorize endless automatic ID churn after later
ambiguous saves.

Inspect disabled workflows with
`workflow_list(project_name, enabled_only=false, include_validation=false)` or
an accessible editor; `capability_describe`/project-scoped `workflow_contract`
currently return 404 for disabled workflows. A listing establishes existence,
not that the timed-out call created it or that its contents match the submitted
bundle. Use permitted content/provenance inspection when available; otherwise
report that uncertainty separately from observed existence. Only a confirmed
save outcome supports reporting creation. Return the actual saved ID,
`enabled=false`, returned `editor_url`, unresolved issues and unperformed checks
when those fields were actually observed; do not invent an editor URL or infer
save success from a conflict/listing.

Honor already granted authorization for enablement, execution, scheduling,
installation and publication. The current MCP authoring surface has no enable,
schedule or test-run action; `workflow_test_run` and `schedule_set` are not
provided. Consult live tool discovery for actual available operations. If an
authorized action lacks a callable surface, report the capability limit and
appropriate next step without asking for that permission again or claiming
completion. The editor's Test Run rejects disabled workflows; enablement through
an available authorized surface must come first. Validation does not establish
external-provider verification, and Test Run does not guarantee blanket
suppression of external mutations. Report observed execution separately from
validation.
