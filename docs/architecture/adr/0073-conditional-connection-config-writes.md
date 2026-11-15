# ADR 0073: Conditional connection configuration writes

Status: Accepted for 0.9.18

Date: 2026-11-15

## Context

An extension may need to establish a connection configuration value once, with
safe same-value retries. Reading an invocation snapshot and returning
`project_connection_upsert` cannot implement that contract: two simultaneous
first writes can both observe an absent value and overwrite one another.

The existing platform configuration patch action has no atomic precondition and
is not a response-envelope host mutation. Project state revisions do not provide
a conditional write of an initially absent connection configuration field.

## Decision

Introduce the provider-neutral host mutation
`project_connection_config_compare_and_set`, granted only to `owning_extension`.
It is available from extension API 1.9.0. The callback contract change owns the
single advertised host API version bump for this release.

An action declares this mutation in its handler and in `runtime_grants.mutations`.
Older hosts reject the unknown grant at manifest compilation. There is no
fallback to unconditional upsert and no optional precondition on that old kind.

Payload:

```yaml
connection_id: selected_connection # optional when supplied by the host invocation
extension_install_id: selected_install # optional, must match host scope
expect:
  - path: account_anchor
    state: absent_or_equals
    value: account_123
set:
  account_anchor: account_123
  selected_account: account_123
```

`expect` is a nonempty list. A condition uses one of:

- `absent`: the key does not exist; `value` is forbidden.
- `equals`: the key exists and equals the required `value` using JSONB equality.
- `absent_or_equals`: the key is absent or equals the required `value`.

An explicit JSON null is a value, distinct from absence. Paths contain dotted
object keys, with at most sixteen distinct paths across `expect` and `set`.
Each path has at most 256 characters and sixteen components; assigned JSON
values have at most 64 nesting levels and must be valid JSONB values.
`set` is a nonempty object of path-to-JSON-value assignments. Empty components,
duplicate expectations and overlapping assignment paths are invalid. Traversal
through a non-object is rejected rather than discarding the existing value.
No array-index assignment or removal operation is introduced.

Paths equal to `oauth` or beneath `oauth` are rejected in both `expect` and
`set`. This host-owned block cannot be read through conditions or overwritten
through the new mutation. The payload cannot modify secrets, secret references,
connection ownership, credentials, enabled state or other columns.

Within the existing host mutation batch transaction:

1. Resolve an exact connection and installation from trusted invocation scope;
   reject conflicting payload identifiers and missing scope.
2. Lock the connection row using connection, account, project, extension and
   installation identifiers with `SELECT ... FOR UPDATE`.
3. Evaluate expectations against the locked, current configuration. Apply the
   assignments to a copy of that configuration and validate the resulting
   connection partitions with the native validator.
4. Update only `connection_config` and `updated_at`. Do not call the ordinary
   connection upsert, which carries unrelated mutable columns.

Equality reads the field from the locked JSONB in SQL. Persistence applies
`jsonb_set` assignments to that original JSONB, including creating missing
object parents. Never serialize the complete decoded Python configuration back
to storage: doing so can round untouched PostgreSQL numbers, even inside OAuth.

No external calls occur while the row lock is held. An OAuth refresh completed
after the extension's snapshot but before this transaction is preserved because
the patch starts with the locked row's current configuration.

A failed expectation returns `host_mutation_precondition_failed`, the condition
path and an `absent_expected` or `value_mismatch` reason. It does not expose the
whole configuration. Any failure rolls back the entire mutation batch and
returns an empty `applied` list. The normal host test-mode suppression remains
responsible for preventing action response side effects during Test Run.

## Consequences

Two first writes for different values have at most one winner. Replaying an
`absent_or_equals` write with the established value succeeds; attempting a
different value conflicts without changing the connection.

Connection schemas must allow native host-owned OAuth metadata when applicable;
this mutation does not bypass native schema validation. Extensions still own
their provider-specific account checks and field meanings. The host has no
knowledge of external advertising or messaging systems.

No database schema migration, new UI, secondary state store or additional
external-effect ledger is needed. The separate ordinary-upsert stale OAuth
snapshot issue is outside this decision.

## Verification

The real PostgreSQL host-envelope regressions cover first write, concurrent
different writes, same-value replay, established-value conflict, absence versus
null, full-batch rollback, schema rejection, ownership scope, protected OAuth
paths and preservation of refreshed OAuth metadata and unrelated columns.
Malformed payload, nested path and JSON equality boundary cases belong to the
same mutation's regression scope. Existing manifest grants and action test-mode
suppression consumers remain part of verification.

Short focused Tier 1 checks use Docker locally; Tier 2, Tier 3 and heavy scopes
run through the canonical GitHub workflow. Record exact candidate SHAs and
selected checks; a single backend diagnostic is not cumulative release evidence.

## References

- [PostgreSQL row locks](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)
- [PostgreSQL JSON operators](https://www.postgresql.org/docs/current/functions-json.html)
