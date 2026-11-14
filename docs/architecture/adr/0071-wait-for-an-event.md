# ADR 0071: Wait for an event

- Status: Accepted (owner decision 2026-11-14: in 0.9.18)
- Date: 2026-11-14
- Builds on: [ADR 0056](0056-pre-1.0-released-database-upgrades.md) (the migration),
  the durable waits of the Wait, Approval and Invoke steps (`wait_step_runtime.py`,
  `workflow_wait_projection.py`, `invoke_workflow_child_wait.py`)

## Context

A workflow can pause durably for a time (Wait), a person (Approval) or a child run
(Invoke). It cannot pause until something outside happens. An extension that starts
long external work (a crawl, an export, a provider job) learns hours later, through
its callback, that the work finished. Today the only reaction is a separate workflow
started by that callback, which does not know which run asked for the work and
cannot hand the result back to it. A person moves the files by hand.

What exists and must be reused:

- Every durable pause has one shape: the hub records `pending_wait` (kind,
  `wait_id`, execution identity) in the checkpoint before dispatch; the node pauses
  with a LangGraph interrupt; the worker projects the pause onto the job row
  (`status awaiting_*`, `jobs.meta.workflow_wait`); the control process turns a
  wake into one stored `workflow_resume` intent (`resume_pending`) and publishes
  it; the existing resume worker delivers it to exactly the wait it names
  (`WAIT_RESUME_APPLIED_KEY`), and the node accepts only its own wake.
- Extension responses (`ok`, `result`, `commands`, `events`, `project_state_updates`,
  `host_mutations`) are processed by the host in one place,
  `response_envelope.process_project_state_updates`, which every caller that runs an
  action with a trusted account, project and extension already calls: callbacks,
  workflow steps, page actions, dataset actions, notification delivery.
- Extension callbacks already give an external service a signed inbound path
  (`hmac_sha256` with a delivery id and replay refusal, `hub_signature`,
  `shared_secret_header`) bound to one project connection.

## Decision

### The step

An event wait is a Wait step with `override.wait.event`; there is no new step kind,
so lanes, Invoke from a lane, the builder palette and the canvas treat it as a Wait.

```yaml
override:
  wait:
    event:
      source: acme.crawler                  # extension id; required
      event_types: [run_completed, run_failed]  # required, 1..20
      correlation_key: state.crawl.run_id   # required; state path, {{template}} or literal
    timeout_ms: 86400000                    # required, 1 ms .. 365 days
    output_mapping:                         # optional
      payload: state.crawl_result
      event_type: state.crawl_outcome
```

- `duration_ms` and `input_mapping` are refused next to `event`; `timeout_ms` is
  refused without it.
- The correlation key is resolved once, when the step is entered, like a mapped
  duration: it must be a non-empty text or integer of at most 256 characters. A key
  that does not resolve fails the step with `workflow.wait.correlation_key_invalid`
  under its error policy, and a retry policy escalates instead of repeating it.
- The step's result is the event: `event_type`, `correlation_key`, `payload`,
  `received_at`, `source`. `output_mapping` copies any of them to state with the
  ordinary step output mapper (the same as Approval's).

### One durable-wait shape

- Kind `event`, job status `awaiting_event`. `pending_wait` carries `source`,
  `event_types`, `correlation_key`, `timeout_ms`, `due_at` and `due_epoch_ms`
  besides the common identity, and the job projection mirrors them in
  `jobs.meta.workflow_wait`. `awaiting_event` is one of `DURABLE_WAIT_STATUSES`:
  it holds no worker, is not stuck, is cancellable, refuses restart and delete like
  the other waits, and appears in the "Waiting" status group.
- The resume reason is `event`. Its value is
  `{reason: event, wait_id, event_id, event_type, correlation_key, payload, received_at}`
  or, at the deadline, `{reason: event, wait_id, timed_out: true}`. The graph resume
  runtime delivers it like a timer or child wake: only to the pause that names the
  `wait_id`, at most once per checkpoint.
- The node accepts an event wake only for its own `wait_id`, a listed event type and
  the exact correlation key; anything else pauses again (the same rule as timer and
  child wakes).

### Delivery: an inbox, matched by the control process

An extension resumes waits by returning `resume_waits` next to `events` in a
successful response:

```json
{"ok": true, "result": {}, "resume_waits": [
  {"correlation_key": "run-42", "event_type": "run_completed", "payload": {"rows": 120}}
]}
```

The host stores each item in `workflow_wait_events` (one row per project, source,
correlation key and event type) and returns; it never resumes a run in the request.
The control process, in the same one-second scan as timers and child wakes, matches
unconsumed rows to `awaiting_event` jobs and, in one transaction, marks the row
consumed by that job and wait and stores the `event` resume intent. Publication then
uses the existing resume queue, retries and republisher.

The inbox is what makes the order of events irrelevant: a fast external service may
answer before the run reaches its Wait step (the key is usually the id the previous
step returned). The event waits in the inbox and is matched when the run parks.

Rules, checked in the database statement and in the node:

1. **Same extension, same account, same project.** The host takes the extension id,
   account and project from the trusted invocation context, never from the response.
   A row matches only a job of that account and project whose wait names that
   extension as `source`. The resuming install is the one serving that project (a
   callback's registration, a step's resolved connection or install), so another
   extension, another account's install or another project's callback cannot reach
   the wait.
2. **Exact key.** Text equality of the stored correlation key; no patterns.
3. **Listed type.** The event type must be one of the wait's `event_types`.
4. **At most once.** The claim moves the job out of `awaiting_event` and marks the
   row consumed in the same transaction (`FOR UPDATE SKIP LOCKED` on both); each
   job takes at most one row and each row goes to at most one job. A second
   delivery of the same project, source, key and type is refused by the unique key
   and logged as a duplicate, before or after it was consumed. A wake that reaches a
   checkpoint already past its pause is ignored by `WAIT_RESUME_APPLIED_KEY`.
5. **Bounded, redacted payload.** `payload` must be a JSON object; it is redacted
   with the shared credential redaction and refused above 64 KiB of JSON. A refused
   item does not fail the action; the response's `wait_events` result names it.
   A storage failure does fail the action, so a callback answers 502 and the
   provider retries.
6. **Retention.** Rows are deleted 7 days after they were received, consumed or
   not. Within that window a key and type are delivered once.

A late event (after the timeout, a cancel or a resume) stays unconsumed and expires;
it never reaches a later pause of the same run because its key and type are bound to
one row and the run is no longer `awaiting_event` for that wait.

### Timeout, cancel, restart

- The same scan claims `awaiting_event` jobs whose `due_epoch_ms` has passed and
  stores `{reason: event, wait_id, timed_out: true}`. Matching runs first, so an
  event that arrived before the deadline wins even when both are due in one pass.
  The node fails the step with `workflow.wait.event_timed_out`; the step's error
  policy decides: fail (default), continue, route to a step (the explicit timeout
  route) or retry (a new attempt waits again with a new deadline).
- Cancel is the ordinary job cancel: the job leaves `awaiting_event`, so no claim
  selects it again, and an event delivered later expires unconsumed.
- Restart safety: the deadline and key are fixed in `pending_wait` at dispatch and
  never move when the node re-executes; the worker projection is rebuilt from the
  checkpoint by the existing pause projection; a stored intent that was not
  published is republished by the existing outbox; a redelivered wake is ignored by
  the applied-wake marker.

### Signed webhook resume: not added

A separate signed webhook addressed to waits was considered and not added. It would
need its own secret per project or per wait, a URL scheme, replay protection and a
second way to name the trusted extension; extension callbacks already provide all of
that (`hmac_sha256` with delivery id, `hub_signature`) and bind the request to one
project connection. A service that should resume a wait posts to the extension's
callback, whose action returns `resume_waits`. Workflow webhooks keep starting runs
only.

### UI

The job shows "Waiting for <types> (key <key>) since <started>, times out at <due>"
with the status "Waiting for event". The builder's Wait step offers "Until an event"
beside the two duration modes.

## Consequences

- New table `workflow_wait_events` and two partial indexes on `jobs` for
  `awaiting_event` (correlation key, deadline), through migration
  `20261114160000_workflow_wait_events.sql` and the baseline (ADR 0056).
- The extension response envelope gains `resume_waits`. Extensions that do not
  return it are unchanged.
- Core knows no concrete extension: the source is data in the step.
- Not covered: a wait on several keys, a pattern key, events from Core itself (job
  events already have workflow reactions), and a readiness check that the source
  extension is installed in the project (a missing extension only means no event
  arrives and the timeout applies).
