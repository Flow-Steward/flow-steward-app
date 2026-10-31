# Runs, Approvals and Reports

Answer operational questions with the job and report tools. Resolve names
through `project_list` and `workflow_list`, and keep the exact ids the tools
return. An omitted filter means the caller's whole scope: never drop a filter
the user asked for, and never widen a query because a narrow one failed.

## Find runs

- `job_list(project_id, workflow_id, status, search, page, per_page)` lists
  runs within the caller's scope. `status` takes one status or a comma list.
  Prefer it to `project_jobs` for anything beyond a glance at recent runs.
- `job_list(..., group_by="attention")` groups runs for triage (failed,
  stuck, waiting). Treat it as a snapshot unless the response says it is
  complete.
- Read the paging and truncation fields every response returns. Follow the
  pages to the end before you state a total, and say so when the answer covers
  only part of the runs or when the inventory changed while you read it.
- `job_list` has no date filter. For a period, filter the returned timestamps
  yourself and say which timestamp (queued, started or finished) you used.

## Inspect one run

- `job_status` for a bounded current summary; `job_result` for the final or
  partial result bundle. Judge success by the job's status, not by a generic
  completion flag, and note when messages or logs in the bundle are cut.
- On failure: `job_errors`, `job_incident`, `job_trace`; for detail,
  `job_messages`, `job_logs`, `job_events` with their cursors;
  `job_runtime_journal` and `job_decision_overrides` when the question is about
  routing decisions; `job_cost_breakdown` for tokens and cost.
- `workflow_stats(project_name, workflow_id)` for a workflow's run statistics.

Present job ids, the observed status, the error or gate and the next useful
action. Separate a run that is still going from a finished one.

## Approvals

- `job_approvals_pending` lists runs waiting for a human decision in the
  caller's scope; narrow it to the requested project or workflow yourself.
  `job_approval(job_id)` shows the active gate, `job_approvals(job_id)` its
  history.
- `awaiting_timer` (a Wait step) and `awaiting_child` (a parent waiting for a
  child workflow) are not approvals. A parent's pending decision lives on the
  child job it links to.
- Decide only with the human's actual decision:
  `job_approval_decide(thread_id, approval_id, approved, idempotency_key,
  comment, action)` with the gate's own `thread_id` and `approval_id` and a
  stable `idempotency_key` reused on retry. Read `allowed_actions`; an
  exhausted rework budget offers only `approve` and `abort`. Permission to
  start a run is never permission to approve it.

## Cancel, restart, acknowledge

Only when the user asks, and after `job_status` shows the run's state:

- `job_cancel(job_id, comment)` for a pending or running job.
- `job_restart(job_id)` re-runs a failed or cancelled job with the same
  inputs. Never restart a run that is still active or waiting (timer, child,
  approval): it would start a duplicate. If the tool refuses, report its
  reason.
- `job_acknowledge`/`job_unacknowledge` mark triage state; they do not fix
  anything.

## Reports

`report_generate(report_type, params, persist)`:

- Operational types `activity_daily`, `project_summary`, `project_stats` take
  `params.project_id`, an exact `params.workflow_id`, and either
  `params.range` (default `7d`) or ISO `params.start_date`/`params.end_date`.
  Job types `job_summary` and `job_cost_breakdown` need `params.job_id`.
- `range="7d"` is a rolling 168-hour UTC window. Explicit dates are inclusive
  UTC calendar days. For a local "today", convert the user's local day to UTC
  boundaries; UTC-day aggregates cannot prove a local-day total, so label them
  UTC or use complete job pages filtered by timestamp.
- `persist=false` skips storing the report but still needs report write
  permission. `report_get(report_id)` reads a stored report.
- These are activity reports, not financial statements; do not derive spend
  totals from incidental cost fields.

Return the actual window, filters, coverage, report id when stored, and the
limitations together with the results.

## Notifications

`project_notification_routes` and `project_notification_deliveries` show where
a project's notifications go and whether they were delivered.
`dispatch_project_notification` sends one; it reaches real recipients, so use it
only when the user asks.
