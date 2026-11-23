# Crash-safe connection polls: implementation notes

Generic Core capability for every extension that polls an external system in the
background (`background.kind: connection_poll`) and starts workflows from what it
finds. Landed in release batch 0.9.24. The guide for
extension authors is the "Background Connection Polls" section of
`docs/extension-authoring-agent-playbook.md`.

## What changed

### Schema (`db/schema/`, migration `20261122130000_connection_poll_delivery.sql`)

- `project_connections.state_version bigint DEFAULT 0` and
  `config_generation bigint DEFAULT 1` (part 02).
- Trigger `trg_project_connections_config_generation` (part 12). Every UPDATE that
  changes one of these moves `config_generation` in the same statement, so it
  holds the row lock:
  - `enabled`;
  - `connection_type`;
  - `connection_config` outside its `oauth` block;
  - `oauth_authorization_revision` (a reconnect).

  A token refresh rewrites only the `oauth` block, `secret_refs` and
  `credential_revision`, so it does not move it.
- `provider_event_inbox`: one row per provider event, unique on
  (`project_id`, `provider_key`, `source_id`).
- `provider_event_dispatches`: one row per workflow a provider event starts.
  - Primary key `delivery_key` (`project_id|workflow_id|task_id`).
  - Status `pending`/`accepted`/`failed`.
  - Records the connection and generation the event was polled under.
- The migration repeats the same DDL idempotently. It is timestamped after the
  batch's `20261122120000_user_authenticator_policy.sql`.
  `db/released_migrations.json` records its checksum and the combined
  `baseline_schema_fingerprint`.
- `schema_contract_catalog.py` requires the two tables and two columns, so a
  process on an unmigrated database refuses to start.

### The poll keeps its connection; compare-and-set in one locked transaction

- The worker passes `connection_id` in the result handler's `audit`, and a
  `poll_context` (`connection_id`, `state_version`, `config_generation`,
  `mode_field`, `mode_default`) read before the call.
  `project_connection_upsert` and project-state writes now go to that connection,
  not to the newest one of the install.
- New `core/application/extensions/connection_poll_commit.py`.
  `_process_extension_response_envelope(..., poll_context=...)` runs
  `_commit_connection_poll_result`:
  1. `SELECT ... FROM project_connections ... FOR UPDATE`;
  2. the verdict:
     - connection missing or disabled, poll mode off, or generation moved:
       `stale_configuration`, and nothing is written;
     - `state_version` moved: `stale_state`, the events are written and the
       cursor is not;
     - otherwise both are written and `state_version` moves on;
  3. the state write runs in a savepoint (a failed write keeps the events), and
     each event runs in its own savepoint;
  4. commands are dispatched after the commit.
- A state mutation may echo `expected_state_version`/`expected_config_generation`.
  They must equal the poll context, or, outside a poll, the row locked in that
  transaction.
- Configuration changes take the same lock, in three places:
  - the DB trigger above;
  - `project_connection_ingress_patch`. It now targets the selected connection
    (`target.connection_id`), locks it, and moves the generation when a written
    field changes value, except a background poll's `interval_field`;
  - a `project_connection_upsert` outside a poll that changes a declared
    `mode_field`.

### Per-type filter and per-connection credentials

- `connection_type_ids` on an action is carried through action-manifest
  compilation. A `connection_poll` action that declares it is called only for
  connections of those types.
- New `core/application/extensions/background_poll_connection.py`, injected into
  the worker loop through `BackgroundPollPorts` (the layer budget forbids
  `core.application` imports in `worker_inbound_polling.py`). It resolves
  `target.connection` with the operations path (`_direct_action_connection_target`).
  - **Types with their own secrets:** the secrets are checked against the type's
    `secret_schema.required`. A missing one skips the poll, and
    `ingress_config.background_poll_status` shows the reason.
  - **OAuth types:** they get the resolved, refreshed access token.
    - A revoked grant (`oauth_authorization_required`/`oauth_needs_attention`) is
      recorded by the OAuth runtime, and the connection is skipped while its
      `oauth.lifecycle_status` says so.
    - A temporary failure (`oauth_provider_unavailable`) skips only until the next
      interval (kept in memory) and changes no status.
- Actions without `connection_type_ids` keep the action-level
  `required_provider_secrets` check (unchanged behaviour).
- `target.connection` also carries `ingress_config` (with declared defaults
  filled in), `state_version` and `config_generation`.

### Exactly one job per event and workflow

- New `core/application/workflows/provider_event_delivery.py`:
  - `record_provider_event` writes the inbox row and one pending dispatch record
    per matched trigger. When a transaction is open (the poll transaction), it
    writes inside it.
  - `dispatch_provider_event` starts the workflow in one job transaction, through
    `enqueue_workflow_start(..., job_created_hook=...)`. The hook does, in order:
    - claims the record (`FOR UPDATE SKIP LOCKED`, must be `pending`);
    - locks the connection (`FOR UPDATE`) and checks that it is enabled and still
      has the record's generation;
    - checks that the workflow (`FOR SHARE`) and its trigger (`FOR SHARE`,
      enabled) still exist;
    - writes `provider_webhook_deliveries` as `accepted` with the job id, and
      marks the record `accepted`.

    A refusal rolls the job back and marks the record `failed` with the reason
    (`trigger_inactive`, `config_generation_changed`, `connection_disabled`, and
    so on). A transient error keeps it `pending`: attempts go up by one, and after
    10 it becomes `failed`.
  - `sweep_pending_provider_event_dispatches` picks pending records older than
    30 s with `FOR UPDATE SKIP LOCKED` and sends each through
    `dispatch_provider_event`.
- `job_created_hook` is threaded through `enqueue_workflow_start` (passed on only
  when given) and the queue adapter `task_enqueue_runtime.enqueue_task`.
- Inside the poll transaction, the workflow starts and the notifications for a
  polled event wait for the commit (`register_transaction_after_commit`). A
  replayed `source_id` records, notifies and starts nothing.
- The worker runs the sweep at start-up and every 60 s
  (`_start_provider_event_dispatch_sweep_loop`, composed in
  `worker_event_ingress.build_provider_event_dispatch_sweep`).
- `start_provider_event_workflows` keeps its signature, and now records and then
  dispatches for every caller, webhook ingress included.

### tool_provider events

An event in `supported_events` returned by a background action takes this path:
`_route_extension_event_through_event_command_service`, then
`ingest_provider_events`, then `start_provider_event_workflows`. `source_id`
survives `normalize_ingress_provider_event` in the payload. The poll path passes
`connection_poll` (connection and generation) through `ProviderEventIngestInput`,
so these events follow the ordering above.

### Poll mode off by default

- A connection type's `ingress_schema` property `default` is written into
  `ingress_config` when the connection is created, both in
  `ProjectConnectionService.create_connection` and in the OAuth grant commit
  (`create_oauth_connection` now inserts `ingress_config`).
- The worker treats a missing mode as the declared default. A type that declares
  none keeps `auto`, so existing extensions are unchanged.

## Design choices

1. **Events go to a new inbox table, not `domain_event_ledger`.** Uniqueness on
   (project, provider, `source_id`) lives on `provider_event_inbox`. The ledger
   row is still written once per new event. A unique index on the generic ledger
   would have changed every other event writer.
2. **A database trigger moves `config_generation`** for disable, reconnect and
   settings edits, so every write path (UI, API, OAuth) is covered.
   - The poll mode and sync scope live in `ingress_config`, which also holds the
     extension's cursor, so the trigger cannot tell them apart.
   - For those, the bump happens in `project_connection_ingress_patch`, and in a
     non-poll `project_connection_upsert` that changes a `mode_field`.
3. **The connection lock in the job transaction is taken after the job row is
   inserted** (the hook runs after `JobSubmissionRepository.create`). The decision
   still happens under the lock, before commit, so the switch-off boundary holds.
4. **The "trigger still active" re-check covers both switches.** One is the
   connection's generation and enabled flag. The other is the workflow trigger
   row, read `FOR SHARE`, so disabling a trigger waits for an in-flight job.
5. **Credentials are checked against the connection type's
   `secret_schema.required`**, so Core names no provider's secret keys.
6. **`project_connection_ingress_patch` uses the selected connection** and takes
   its type from it. Before, it picked the newest connection of the install. A
   patch whose `connection_type_ids` excludes the selected type fails with
   `invalid_connection`.
7. **Retry of a failed delivery.** The old webhook path could re-claim a `failed`
   delivery when the provider resent it. Now a transient failure stays `pending`
   for the sweep (up to 10 attempts). Final refusals stay `failed`, and a resend
   of the same `source_id` does not re-arm them.

## Tests (Tier 1, Docker `fs-audit` with `pg-audit`)

- `tests/integration/test_connection_poll_delivery_postgres.py`:
  - state and the compare-and-set:
    - independent cursors in both orders;
    - two workers, one state write;
    - switch-off and scope change while the poll waits;
    - an interval change keeps the generation;
    - disable and settings edits move the generation, a token refresh does not;
    - both race interleavings, with a pause after the poll lock;
  - delivery:
    1. crash inside the poll transaction;
    2. crash inside the job transaction;
    3. crash right after the job commit;
    4. the same page delivered twice;
    5. two workers at once;
    6. two workflows on one event;
    7. a pending record after the trigger is switched off;
    8. switch-off races, in three variants: connection switch-off before the job's
       lock, trigger disable before the job's check, and a job committed before
       the switch-off;
  - the extension's `source_id` on the inbox row and in the job's `task_id`.
- `tests/unit/infrastructure/runtime/worker/test_worker_background_poll_types.py`:
  - the type filter;
  - an OAuth poll without any app credentials;
  - a connection with a missing secret, skipped with a status;
  - revoked and temporary token failures;
  - a new connection that is not polled.
- `tests/unit/test_background_poll_connection.py`: the resolver (own secrets,
  refreshed token, revoked, temporary, missing secret) and ingress defaults,
  including the OAuth create path.
- `tests/unit/test_project_connections.py`: `create_connection` writes the
  declared defaults.
- `tests/unit/test_provider_event_trigger_dispatch.py`: rewritten for
  record-then-dispatch.

The evidence (commit, selection, result) is in the hand-over report. Not run:
Tier 2/3, the full suite and Sonar. Those run hosted only, on an explicit request.

## Follow-ups

- An ADR for this pattern: a connection poll's compare-and-set, the event inbox,
  and job creation together with its delivery record. The number is assigned by
  the coordinator.
- `background_poll_status` is visible only through `ingress_config`. An extension
  page has to read it to show it.
- Retention: `provider_event_inbox` and `provider_event_dispatches` are never
  pruned. A cleanup job is still to do, for example one that removes accepted or
  failed rows older than 30 days.
