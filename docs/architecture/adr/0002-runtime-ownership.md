# ADR 0002: Runtime Ownership

- Status: Proposed
- Date: 2026-04-12

## Context

Flow Steward runs responsibilities across multiple processes and execution paths.
Without a fixed ownership model, loops, transports, and execution behavior can
drift into overlapping runtimes and create hidden coupling or duplicate work.

## Decision

Runtime ownership is fixed as follows:

- `app` owns HTTP transport, HTML rendering, WebSocket flows, and transport-coupled orchestration entrypoints
- `control` owns singleton control loops
- `worker` owns stateless background execution, including durable dispatch and lease-based recovery of approval-resume intents accepted by API, CLI, admin, or MCP transports
- `mcp` owns the inbound MCP transport adapter for projects, workflows, jobs,
  reports, and notifications. Tools forward the caller's
  bearer token to the application API; application services own domain logic
  and the API enforces role, capability, tenant, project, and workflow policy.
  Request-local tool discovery uses the canonical token-scope mapping; it is
  not a substitute for authorization on each call.

The built-in MCP surface includes read and mutating tools, not a separate
knowledge service or code-indexing engine. Repository code search, code graphs,
schema-index snapshots, and automatic/manual project reindexing are not core
transport responsibilities. The public CLI is an operator adapter over
application services; it does not introduce another domain or release engine.

Outbound MCP resource ownership:

- For normal queued graph execution attempts, the worker's application execution
  lifecycle activates process-local, execution-scoped MCP client and
  legacy-session ownership.
- That lifecycle closes the attempt's owned resources on success, failure,
  cancellation, timeout, `GeneratorExit`, and suspension for approval. Worker
  restart and queue redelivery begin a new execution scope.
- Approval resume currently uses connector-local RPC contexts. Each context
  creates a fresh client and closes it on context exit.
- API, CLI, admin, WebSocket, and MCP transports do not own this cleanup.
  Protocol clients and session state are not carried across execution attempts;
  session identifiers or negotiated protocol state are not persisted as job or
  workflow state.

Approval-resume ownership:

- API, CLI, admin, and MCP transports record an approval decision as an
  application-owned durable intent; transports do not resume the graph directly.
- `core/application/jobs/workflow_resume_state.py` owns the state contracts for
  pending, published, running, retryable, finalizing, and completed resume
  revisions; infrastructure database adapters implement the fenced SQL
  transitions behind those contracts.
- Workers may execute a resume only after claiming a fenced execution lease and
  must renew that lease while the graph is running. Completion from `running`
  requires the current, unexpired execution lease for that resume revision.
- Entering `finalizing` clears the execution lease. Terminal side effects and
  completion then use a separately claimed, renewable finalizer fence for the
  same revision. Completion from `finalizing` requires the current, unexpired
  finalizer token and a fully completed finalization journal; the old execution
  token cannot complete that state.
- Worker terminal side effects remain worker-owned; resumed terminal jobs must
  run the same cleanup, scheduled-lock release, and notification paths as normal
  queue executions.

## Consequences

- New responsibilities must be assigned to one owner runtime.
- Duplicated ownership across runtimes is a design issue.
- Any new runtime responsibility requires an ADR update or a new ADR.

## Alternatives Considered

- Mixed runtime ownership based on implementation convenience
- App-owned control loops by default

## Rollout Notes

Continue converging touched areas to the existing runtime map rather than moving everything at once.

## Related Standards and Docs

- `docs/standards/architecture-standard.md`
