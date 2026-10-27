# ADR 0026: Provider-Neutral Operation Retry Ownership

## Status

Proposed — 2026-09-09

Status note (2026-10-26): clause 9 and the last consequence now describe For
each as defined by [ADR 0038](0038-workflow-containers-for-each-collect-hooks.md):
the inline `for_each` executor is gone, and a body step is an ordinary step
execution of its item frame.

## Context

Production outbound HTTP is issued through workflow HTTP steps, extension
operations, remote runtimes, MCP and OAuth control exchanges, and the public
extension SDK. Several adapters previously repeated ordinary reads locally,
while the supervisor also retried failed phases. That created stacked retry
budgets, inconsistent delays, and a risk that a mutation with an unknown result
would be replayed.

Core cannot safely interpret provider-specific response bodies or error names.
Extensions cannot safely choose the workflow retry budget or deadline. Both
sides need one narrow contract that preserves this ownership boundary.

## Decision

1. The Core orchestrator is the sole owner of operation-level retry budget,
   scheduling, delay, jitter, deadline enforcement, and telemetry. The default
   budget is two additional retries after the first invocation, for at most
   three invocations. Operation retry is a distinct `retry_operation` route;
   phase-level validation and rework continue to use `retry_phase`.

2. A provider adapter may publish only the closed safe projection:
   `operation_kind`, `request_semantics`, `failure_class`, `retryable`, an
   optional positive `retry_after_seconds`,
   `definitely_no_external_effect`, `external_effect_status`, a bounded safe
   provider error code, and HTTP status. Core ignores arbitrary nested fields,
   URLs, headers, response bodies, and unknown values.

3. Safe reads may retry transient network failures and HTTP
   408/429/500/502/503/504. Mutations are one-shot unless the result proves that
   no external effect occurred or an existing reconciliation protocol owns
   recovery. `timeout_unknown`, `running`, and `succeeded` are never
   automatically replayed.

4. Backoff begins at two seconds, doubles for each retry, is capped by project
   policy, and adds zero through twenty percent jitter. A valid `Retry-After`
   delta or HTTP-date is a minimum delay. A delay beyond the policy cap or the
   remaining operation deadline is terminal and receives a stable reason code.
   The checkpointed `operation_retry_wait` node is the only component that
   sleeps for an operation retry. Core persists one absolute deadline and retry
   counter per stable operation identity before entering that node, recomputes
   remaining time before sleeping, and checks the deadline again immediately
   before dispatch.

5. Provider response parsing remains in its adapter. Canonical retry modules
   must not import extensions, mention provider identifiers, or branch on an
   extension/provider identity. MCP tool annotations are untrusted hints and do
   not authorize replay unless a separately established internal contract says
   otherwise. MCP `tools/call`, OAuth token/code POSTs, SDK uploads, and document-
   conversion POSTs are one-shot control/data exchanges by default.

6. Local repetition is permitted only for a bounded protocol reconciliation
   loop, such as a resumable upload offset probe, MCP session reinitialization
   followed by `tools/list`, or a messaging provider's explicit
   parse-format rejection fallback. It does not consume or enlarge the orchestrator budget.

7. Every production outbound HTTP call site is registered in
   `docs/architecture/outbound-http-inventory.json` with exactly one owner:
   `orchestrator`, `local_reconciliation`, or
   `no_retry_control_exchange`. An AST architecture test compares the live
   call-site set and occurrence counts with the registry; its self-test proves
   that an unregistered caller fails.

8. Retry preserves the original workflow operation and external-effect
   identity. A parallel operation retry resumes only the failed lane at its
   failed step; completed sibling lanes are neither invoked nor merged again.
   ADR 0025 remains authoritative for claim fencing, encrypted replay, and
   ambiguity handling; this ADR does not create a second idempotency system.

9. V1 exact retry applies only to host-dispatched deterministic connector and
   HTTP workflow steps. An agent-managed custom HTTP tool cannot resume an
   immutable tool call yet, so its structured failure is preserved but retry
   is terminal; this prevents replay of earlier agent tool mutations.
   Supporting that path later requires a checkpointed exact-call cursor and a
   new architecture review. A step inside a For each body is an ordinary step
   execution of its item frame (ADR 0038), so the same rules apply to it; its
   retry identity and budget belong to that step execution and its loop item
   (`core/application/orchestration/step_operation_retry.py`).

10. Admissible host transport retries retain the existing identity, counter,
    backoff and absolute deadline. Once technical retry is forbidden, exhausted
    or expired, use the existing authored error handling before missing-response
    regeneration: fail, continued error or error route creates no implicit
    Approval. Authored retry cannot reset the host refusal/count/deadline,
    including a late deadline at dispatch or after a persisted wait. An unknown
    mutation is never automatically resent or promoted as canonical success.

    Human retry uses the existing step-owned direct error edge to an ordinary
    authored Approval and its chosen immediate direct return to the original
    serial connector. Approved/rejected keep their existing graph outcomes and
    comment rules: Approve may omit a comment; ordinary browser Reject requires
    feedback. Token rejection keeps its required reason except for an allowed
    `abort`. No
    additional Automation selector, setting, special workflow modal or public protocol is introduced. Existing
    graph validation and rework limits apply. For the exact bound return, an
    ESCALATE transition because the existing budget is already exhausted
    rejects the decision with the
    existing service conflict before fence/grant/history/outbox writes and
    leaves the same ordinary gate pending. It does not accept the decision and
    open a new budget pause or bypass a limit. Other ordinary budget behavior
    is unchanged.

    ADR 0025 owns the neutral one-use authorization: scoped original observation,
    current gate/execution and actual direct DISPATCH must match the job-pinned
    definition/cursor, checked in the job decision transaction. The ordinary
    canonical effective decision determines the outcome, not action text alone.
    The receipt/fence/resume intent records permission to repeat an uncertain
    request, not provider success or proof of no effect. Only the exact normal
    connector claim with the matching declaration/full original intent consumes
    it; a changed or removed declaration cannot fall through to fresh invocation.

    This boundary covers one ledger-declared serial connector and one same-source
    direct error→Approval→original cycle. Group/For each frames qualify only with
    verified preservation; container exit/re-entry, changed items, lanes,
    multiple unknowns and indirect routes remain ungranted. Raw HTTP without a
    ledger retains its existing graph behavior, including possible redispatch,
    but receives no durable one-use ledger guarantee from this mechanism.

    A separate Lookup may return a real exactly correlated ID for downstream
    continuation. It does not settle the original unknown, fabricate its lost
    output or prove safe resend from an absent result. Local fencing prevents
    stale confirmation, not a late remote write. No provider-proof adapter is
    added; direct Extensions factual recovery remains independent.

    Only the current executor's validated host provenance can select the narrow
    transport path, including the existing trusted agent-managed custom HTTP
    channel. Model artifacts cannot. Cancellation, stale-attempt checks,
    nontransport guardrails/validation, authored rework and autonomy keep their
    existing behavior. UI/API/MCP/CLI/WS still address the same ordinary pending
    gate with unchanged current identity, permissions and transport-specific
    comment requirements.

## Consequences

- Provider adapters remain independently testable and cannot silently multiply
  retry attempts.
- A workflow receives consistent budget, backoff, deadline, terminal reason,
  and safe telemetry across generic HTTP and extension transports.
- Definitive rate limiting can recover without permitting ambiguous mutation
  replay.
- New outbound callers and new local retry loops require an explicit ownership
  decision visible in architecture review.
- Agent-managed custom HTTP operations deliberately do not receive automatic
  operation retry in V1. Steps inside a For each body are retried per loop item
  like any other step execution.
