# ADR 0025: Encrypted External-Effect Replay

## Status

Proposed — 2026-09-06

## Context

Flow Steward suppresses duplicate external effects so a retried workflow step,
command, direct extension action, or notification does not repeat a provider
side effect. Suppression alone is insufficient after the original provider call
succeeds: every consumer must receive the same safe result as the first caller,
without invoking the provider again.

The provider response cannot be stored as ordinary effect metadata. It can
contain personal data, must not become visible through existing audit queries,
and may include undeclared or unsafe host instructions. A successful effect and
its replay value must also become durable atomically; otherwise a crash can
leave a success marker that has no result to replay.

## Decision

1. Core owns one encrypted replay record for each external effect in a separate
   `external_effect_replays` table. Existing external-effect projections and
   `SELECT *` callers do not expose its ciphertext.

2. Replay content is a closed projection produced from the immutable installed
   release operation manifest. Core retains only declared outputs, a small
   protocol-result vocabulary, finalized artifact or dataset summaries, and
   finalized grant handles. Provider commands, events, project state, arbitrary
   errors, undeclared fields, and provider-supplied top-level output handles are
   not replayable. Consumers pass host-finalized outputs to the projector over a
   separate provenance-preserving argument.

   Runtime resolution of the external-effect declaration is strict: duplicate
   declarations in one descriptor source, or conflicting top-level and compiled
   declarations, fail before claim or provider execution.

3. The projector applies bounded depth, node-count, UTF-8 byte, plaintext, and
   ciphertext limits. It rejects protected-value reflection and fails closed on
   malformed, ambiguous, missing, or inconsistent operation descriptors.
   Credential-like keys are rejected recursively even when nested inside a
   declared generic output; benign names such as `token_count` remain valid.
   Before claim, Core validates that the operation has one unambiguous output
   contract with non-empty, unique names and no reserved host-control output
   names.

4. Replay payloads use AES-GCM with a domain-separated key derived from the
   configured encryption root. Authenticated associated data binds the payload
   to the effect, account, project, extension, installation, connection,
   operation, idempotency scope and key, and request fingerprint. A binding mismatch or
   authentication failure is a typed non-replayable result.

   External-effect request, general scope, direct-action intent, workflow
   intent, notification event/route intent, command intent, and rate-limit
   bucket identities are distinct domain-separated keyed HMACs derived from
   the same required encryption root. Core does not publish
   an unkeyed equality oracle for low-entropy request or target values, and
   external-effect claims fail closed when the encryption root is unavailable.

5. The database locks and validates `(effect_id, account_id, claim_token)` while
   the effect is `running`, then writes the encrypted replay and marks the effect
   `succeeded` in the same transaction. A stale or lost lease cannot publish a
   replay or overwrite the winner.

   UUID identifiers are validated at the store boundary and compared as typed
   UUID parameters. Hot replay lookups and effect transitions do not cast UUID
   columns to text, preserving indexable predicates and predictable plans.

   A notification failure proven to occur before provider execution may use the
   same claim token for a fenced `claimed` to `failed` transition. Once the host
   has marked the effect `running`, every terminal transition remains fenced on
   `running`.

   Host setup failures before provider execution transition `claimed` to
   retryable `failed`. Exceptions and reflected-secret failures after the
   provider boundary transition `running` to `timeout_unknown`, clean prepared
   grants, and never authorize an automatic provider retry.

   Host-owned pre-spawn suppression and rate-limit transitions must also win
   their fenced `claimed` update before Core reports a terminal result. Losing
   that fence is a typed failure, never a synthesized suppression or rate-limit
   response.

   A notification provider response with `delivered=false` is uncertain after
   the provider boundary unless the response explicitly and consistently proves
   `definitely_no_external_effect=true`. The uncertain case is
   `timeout_unknown` and is not automatically retried; only the proven
   no-effect case is retryable `failed`. An explicit `timeout_unknown` is never
   weakened to `failed` by notification normalization.

6. Once the provider has reported success, inability to project, encrypt, or
   persist replay is `external_effect_replay_persist_failed`. Consumers must not
   return success and must not invoke the provider again. Core makes a
   best-effort transition to `timeout_unknown` so the uncertain external effect
   remains visible for operator resolution.

   Replay confirmation reports a typed commit outcome: `rolled_back` only when
   rollback is proven, `committed` only after the owning transaction's exact
   after-commit marker, and `unknown` otherwise. Confirmation refuses ambient
   transactions. A committed or ambiguous exception performs one account-
   scoped primary join fenced by claim version/token, all binding identities,
   and the exact attempted ciphertext before deciding whether success is
   recoverable. The safe default is `unknown`.

   Host-minted output IDs are registered durably after grant preparation and
   before an effect becomes `running`. Reconciliation identity includes effect,
   account, claim version, and claim token, so a retry cannot overwrite an older
   orphan and a stale sweeper cannot delete a newer attempt. Atomic success
   resolves only the current attempt. Superseded and expired terminal attempts
   are cleaned by an independent, account-scoped control loop through the
   artifact plane; claimed/running attempts and replay-backed current success
   remain untouched. Failed deletions retain their reconciliation record and
   fair ordering advances attempted work so one bad account or object cannot
   starve the rest.

   `suppressed` is exclusively a host-owned pre-invocation test-mode outcome; a
   spawned provider cannot report it as a successful terminal confirmation.

7. All four external-effect consumers use the same hydration boundary. A
   duplicate returns the decrypted projection or a typed tombstone; no consumer
   reconstructs a replay from the public audit summary.

   Connector session rotation is host control data rather than replay output.
   Core persists it before confirming success, removes it from response and log
   projections, and never reapplies it while hydrating a duplicate.

8. Retention removes ciphertext but preserves a `retention_expired` tombstone.
   Extension uninstall removes ciphertext and preserves an
   `extension_uninstalled` tombstone before detaching the audit record. A later
   reinstall receives a new install-scoped effect identity and cannot inherit
   the prior installation's replay. Per-action stale-lease cleanup retains its
   global fencing responsibility, but replay-retention cleanup is scoped to the
   current account and uses replay creation/update time plus the account policy;
   an action never scans or mutates another account's replay rows.

9. A direct HTTP action that declares an external effect requires a host-owned
   `Idempotency-Key`. Core stores only its digest as the intent identity. A new
   key represents a new user intent even when the payload is identical; a retry
   with the same key replays the first result. Reusing a key with a different
   request fingerprint is a typed conflict and never invokes the provider.
   The fingerprint covers provider-visible direct-action inputs and raw grant
   requests; transport timeout is deliberately excluded because it cannot change
   the provider request or replay value. Manifest-declared business inputs are
   fingerprinted verbatim even when their domain names are `attempt`, `headers`,
   `config`, or `token`; host connection and runtime material is rejected at its
   control boundary instead of by recursive generic-name filtering.

10. Command dispatch uses its durable command id as the default external-effect
    keyed intent. Therefore two otherwise identical commands are independent intents.
    When the caller supplies a correlation id, its digest becomes the stable
    retry identity: an identical retry deduplicates, while reuse with changed
    payload is a typed conflict.

11. The extension manifest UI owns one opaque idempotency key for the current
    logical submission. It retains that key only after a transport ambiguity or
    `timeout_unknown`, so a deliberate retry cannot repeat the provider effect.
    Success, a definitive failure, or an idempotency conflict clears the key and
    the next submission starts a new intent. The UI retains the complete original
    submission only in memory for an exact authorized retry; it never writes the
    payload, secrets, or host context to persistent browser storage. It shows
    explicit guidance when provider completion is uncertain or edited input
    conflicts with the retained intent.

## Operator resolution of an unknown effect

A persisted `timeout_unknown` means the provider may have acted. It does not
permit an automatic resend. Direct Extensions actions use a scoped factual
resolution for the exact effect and observed claim version; the authored
workflow retry authorization below is separate:

- `approve` records that the operation completed and blocks a further provider
  call. Existing real outputs or authenticated replay remain usable; missing
  IDs, responses, artifacts, and dataset handles are never invented.
- `reject` records that no effect occurred and authorizes one exact retry of the
  original intent. An ordinary claim without a validated resolution reference
  stays blocked, and a fresh idempotency key alone grants no retry permission.
  Each submitted `resolution_ref` must match the full original host intent and
  current fence, and its retry permission must be unconsumed. A changed
  payload/connection/install is not that original retry.

Both actions require an authorized actor and a nonblank comment. The resolver
locks the account-scoped effect, rotates its fence, and writes the decision and
original bounded/redacted failure provenance to command audit in one
transaction. A duplicate exact decision returns its receipt; a conflicting
choice loses. A newly loaded client cannot decide again while a recorded
no-effect decision awaits consumption. Audit status `executed` means the
operator's decision was recorded, not that the provider confirmed execution.

The normal claim path validates that receipt, full persisted intent, fence, and
unconsumed authorization before atomically recording consumption and creating a
fresh claim. Only one consumer receives permission to spawn. An unknown commit
outcome grants no spawn permission without proof of durable claim ownership.
A later ambiguous attempt needs a new decision; an old reference cannot authorize
it. Fence rotation prevents a stale worker from publishing a result, but cannot
cancel a network request already sent to the provider. Existing reconciliation
retains the old attempt's identity and fresh retries receive new output grants.

Workflow ambiguity follows the existing step/container error handling; the
default is `fail`, not a forced special gate. A step's own executed error edge
may go directly to an ordinary authored Approval. Only its chosen existing
outcome's immediate direct DISPATCH back to the original serial connector can
issue neutral retry permission. Other outcomes keep ordinary semantics and
clear the private association; ordinary comment rules remain unchanged. If the
exact bound chosen return resolves to an escalation because the existing
rework budget is already exhausted, reject it with
the existing service conflict before effect fence, grant, history or outbox
writes. The same ordinary gate stays pending; do not accept the decision and
create another budget pause. Unrelated ordinary budget handling is unchanged.

The host validates the original scoped observation, source and claim version,
the current authored gate/execution, and the job-pinned definition/cursor.
Decision verification reads the job-owned snapshot inside the existing job
transaction and compares its pin/digest; it never uses the live registry or a
caller-supplied effect/target. The selected outcome uses the same canonical
effective decision as graph resume. The narrow pre-decision association is not
provider-call authority and cannot pass a recorded-receipt check.

Within lock order job then effect, decision/history, neutral retry receipt,
original failure provenance, rotated claim fence and durable resume outbox
commit together. This purpose means “another attempt is authorized despite an
uncertain outcome”; it does not record success or `operator_confirmed_no_effect`.
The receipt records actor, ordinary Approval, chosen outcome, pin/direct target,
full original intent and observed/post-fence versions. Existing factual direct
action purposes remain separate, including their mandatory comment.

The host-owned `external_effect_resolution_ref` crosses existing live and
state/checkpoint resume only to that verified target dispatch. At normal claim,
require the matching ledger declaration and full original request/source intent
before consuming; a removed declaration cannot fall through to ordinary
no-declaration invocation. A changed body/connection/install/item, stale gate or
missing checkpoint cannot consume permission or spawn. Existing one-use
consumption, commit-before-spawn, replay tombstone and late-callback/output
fencing remain authoritative. No new public field, API, table, setting or engine
is required; mappings and extra state cannot create the association.

First support is one ledger-declared serial connector, one direct step-owned
error→Approval→original return in the same source/item. Same-scope Group/For each
frames need explicit preservation proof; inherited container exit/re-entry,
lanes, multiple unknowns and indirect routes are not granted. Raw HTTP without
a ledger retains ordinary execution behavior and gains no such durable fence.
A lookup can pass a real exactly correlated ID downstream, but does not settle
the old unknown or prove safe resend from an absent result. No automated
provider-proof adapter or fabricated response/ID/artifact/handle is added.

The same ordinary gate remains available through existing UI/API, MCP, CLI and
public WebSocket job events, with current identity, allowed actions,
permissions and existing comment rules. Approve may omit a comment; ordinary
browser Reject requires feedback. Token rejection retains its required reason
except for an allowed `abort`, alongside current identity and idempotency checks. Internal
source/checkpoint/ref data is not a public decision input. A required consumer
still fails if real data is missing. A local fence cannot cancel a late remote request.

For direct extension actions, use the same resolver from the existing scoped
result card. The host's safe `action.response.external_effect` association
contains only `effect_id`, `claim_version`, `status`, and `operation_id`; provider
metadata cannot create that association. The scoped summary restores a known
effect after reload without exposing its request, key, source reference, lease,
or replay. Clients retain the complete original request and reuse its original
`Idempotency-Key` with the server-issued resolution reference. The server
restores the persisted host identity and validates the exact intent, current
fence, and unconsumed permission; a new key alone is never retry authority. A restored card
without that request explains why retry is unavailable. No workflow/job is
created for a direct action, and no payload reconstruction or provider polling
is added. An ambiguity without an existing matching project/install connection
binding is not made visible or resolvable by inventing one.

## Consequences

- Duplicate execution has exact, cross-consumer semantics without broadening the
  public external-effect audit contract.
- Provider responses containing allowed personal data remain encrypted at rest;
  secrets and host-control material are never accepted into replay storage or
  plaintext public effect metadata.
- Success confirmation now depends on replay persistence and can fail after the
  provider has acted. That failure is deliberately surfaced as uncertainty,
  because retrying the provider would risk a duplicate side effect.
- Operators retain a durable reason why a previously successful result can no
  longer be replayed, while retention and uninstall still eliminate ciphertext.
- New consumers of external effects must use the shared projector, confirmation,
  and hydration paths rather than defining consumer-specific replay behavior.
