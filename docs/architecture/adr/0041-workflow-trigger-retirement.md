# ADR 0041: Workflow trigger retirement and current-reference credential decisions

- Status: Accepted
- Date: 2026-10-25
- Related: ADR 0035 (workflow interface), ADR 0038 (pinned workflow definition)

## Context

Soft deletion retained enabled webhook/provider triggers and due schedules. The
canonical job definition pin already rejects a deleted owner, but that late fence
does not retire trigger authority or prevent earlier authentication and deferred
provider effects. Registry deletion and unified Trash use different transaction
adapters; registry `store.conn` is not its `_transaction` physical connection.

## Decision

Both deletion adapters call one application lifecycle with their physical cursor.
It locks the exact account-qualified workflow W, then checks nonterminal jobs in a
fresh statement, then locks its entrypoints E in stable identity order. It retires
eligible exact credentials S, writes the tombstone/revision/changelog, disables
webhook and provider triggers, and pauses schedules within the same transaction.
The deletion history before-image reads the current locked workflow and definition
through that same cursor before mutation; pre-lock lookup data is not history authority.
Any failure rolls back every stage. Registry refresh follows commit. Workflow
identity, enabled policy, graph, project links, terminal jobs and default input
`secret_refs` remain. Direct and planned restore do not restore trigger authority.

Established credential version writers lock every retained row for the exact
project/key using `FOR UPDATE NOWAIT`, including inactive versions, and perform a
bounded fresh pass before writing. Lifecycle uses deterministic account/project/key
order and refreshes the complete held set before one SQL inventory statement.
Only current active row IDs accepted by that inventory are retired. There is no
active-only or oldest-row anchor, automatic lock retry, project/global guard,
SKIP LOCKED, missing-key sentinel or first-writer protocol. A dedicated 55P03
conflict escapes the real owner and maps to a nonsensitive 409 after rollback.

The four application version writers participate: webhook create/update, ordinary
callback creation and OAuth capability creation. Ordinary callback uses a local
transaction around guard/version write; foreign metadata protects the committed
credential even before registration. OAuth uses its existing outer transaction.
Same-owner webhook changes lock W before E and reread route-preloaded state so a
stale command cannot rebind or resurrect deleted authority.

Credential candidates come only from persisted top-level `secret_ref` and auth
`secret_ref`/`password_secret_ref` fields, including old refs after key regeneration.
Names/prefixes do not establish ownership. Any foreign/unknown metadata on any
retained version protects the key, as do visible other entrypoints, callbacks,
registry definitions, inactive projections and relevant nonterminal pinned
consumers. Malformed or ambiguous evidence is retained. Separate workflow input
secret defaults are a different authority store and remain untouched.

The single committed-consumer inventory is the credential decision point. Saved
literal references and definition pins do not lease usable credential authority.
A reference-only save overlapping the decision may be ordered after retirement,
even if it physically commits before the deletion transaction. Such a reference
may subsequently fail ordinary resolution. Already visible consumers are
protected. Target-workflow admission still uses its independent W SHARE pin and
nonterminal-job fence; no admitted job is cancelled to permit deletion.

Deleted-owner public entrypoint preparation produces the same early 404 as an
unknown key, before credential reads, body staging, replay or submission. An
active manually disabled entrypoint keeps its 409. Provider candidate queries
exclude deleted owners, and a selected delivery holds W SHARE through auth,
deferred local state, replay and canonical enqueue/pin. Schedules are paused and
fresh due/claim selection excludes unavailable owners without advancing the slot.

Retained workflow-provider configuration owns the workflow channel for the exact
resolved project/provider. Disabled or deleted-owner rows produce unmatched 404
when no eligible trigger matches. Other projects cannot change that routing
choice. Explicit authorized removal of the last retained trigger restores the
independent generic provider flow; there is no permanent routing tombstone.
Narrow include-trashed reads permit authorized exact cleanup without relaxing
create, enable, rotation, regeneration or run admission.

## Limits

This is a supported-application protocol, not a generic credential lease or a
system-wide deadlock-freedom claim. Generated public first keys rely on the
existing strong-random identity contract. Arbitrary direct-storage collisions,
out-of-band writes and callers swallowing errors inside external transactions
are outside that guarantee. Historical unreferenced orphans and unprovable
ownership are retained, not prefix-purged or backfilled. Source-level tests and
synthetic signed delivery do not establish installed-provider runtime acceptance.
