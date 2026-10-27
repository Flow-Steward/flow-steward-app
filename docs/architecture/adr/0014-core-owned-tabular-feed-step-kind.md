# ADR 0014: Core-Owned Tabular Feed Step Kind

## Status

Accepted for pre-1.0.

## Context

`tabular_feed` began as a first-class workflow step whose heavy execution was
delegated to the bundled first-party extension identified historically as
`flowsteward.tabular-feed`. That split made the user-facing step look built-in
while generic tabular parsing, Polars execution, snapshot persistence, and
Artifact/Dataset Plane access lived behind an artificial extension boundary.

This created unnecessary runtime indirection for a first-party bulk-data
primitive, complicated authorization and storage compatibility, and made the
Builder inspect path depend on extension-action dispatch even though no external
provider or transport was involved.

## Decision

`tabular_feed` is a built-in first-party workflow step kind owned by core.

Core owns:

- workflow routing, readiness, validation, state result projection, and Builder
  support;
- tabular action and relational contracts;
- Polars-backed execution, file format readers/writers, and streamed storage
  transfer;
- Artifact/Dataset Plane ownership checks, snapshot heads and commit receipts,
  candidate metadata, checksums, and immutable dataset publication;

Extensions remain responsible for external transports, providers, sources, and
destinations: FTP/SFTP/FTPS, provider APIs, notification providers, Google Sheets,
marketplaces, and other integration-specific behavior. They compose with
`tabular_feed` through artifact and dataset handles. Extensions do not own
generic artifact/dataset-backed tabular processing.

Extension-contributed workflow step kinds are explicitly out of scope for this
decision. This ADR does not introduce a plugin mechanism for adding arbitrary
new step kinds or executable tabular actions.

## Compatibility

The persisted step kind remains:

```text
step_kind: tabular_feed
```

The persisted `override.tabular_feed` format and existing operations remain.
`diff.snapshot_mode` chooses `compare` (the default) or `publish`. The Builder
shows **Comparison purpose** using the existing typed select; publication does
not require a user-entered revision, artifact ID, or reference object. Comparison
remains independent of snapshot persistence and produces a noncommittable candidate.

`checksum` is the canonical snapshot comparison operation. Pre-release
extension action IDs are not accepted or migrated.

### Snapshot publication authority

`fs_tabular_snapshot_heads` holds one database head per normalized
account/project/workflow/profile scope. Job, run, and step IDs do not change that
scope. Revision zero has no artifact; every successful new candidate increments
the revision. `fs_tabular_snapshot_commits` stores a durable receipt per scoped
candidate, including its original revision, previous artifact, and result metadata.
Project/account ownership and project deletion are enforced by database foreign
keys. Profile normalization and the existing scope namespace remain unchanged.

`checksum` reads the head reference and checksum metadata together and returns
`committed_snapshot_ref = {revision, snapshot_artifact_id}` alongside the existing
checksum fields. Publication `diff` derives its saved reference from the exact
baseline artifact's durable scoped commit receipt, never from a newer head.
The baseline must be an untouched canonical snapshot Parquet `read` with
host-owned scope and eligibility metadata. Mapped reads and derived or
uncommitted candidate datasets cannot substitute for that baseline.

An initial noncanonical baseline must have actually zero rows to derive revision
zero; claimed row counts are insufficient. An empty **committed** canonical
snapshot retains its positive historical revision, and an empty uncommitted
snapshot cannot acquire revision-zero authority. Candidate metadata saves the
reference before publication. A head advance still causes a commit conflict.

Existing machine configurations may retain a whole `expected_snapshot_ref`
object or whole-field state selector. Without a purpose it implies publication;
if supplied, it must match the same historical receipt or verified initial
empty baseline. The Builder hides this bookkeeping and removes the hidden
reference when the user chooses comparison. Explicit comparison plus a reference
is invalid. No new selector dialect is introduced.

`commit_snapshot` rechecks candidate grants, ownership, scope, metadata, and
artifact availability before locking the head. Under the head lock it checks the
durable candidate receipt first, then compares both the saved revision and
artifact ID. A new commit updates the head and inserts its receipt in one
transaction; a mismatch returns `snapshot_conflict`. Concurrent first commits
share one revision-zero head. If A commits, then B commits, a late repeat of A
returns A's original receipt without moving B's head. Receipt replay still
requires current access to the candidate. No Parquet transfer or destination
call runs under the head lock.

Parquet candidates remain persistent artifacts in the canonical Artifact/Dataset
Plane; the database holds publication metadata. Candidate sidecars retain their
existing key namespace:

```text
tabular_feed/snapshots/<scope>/<profile>/candidates/<artifact>.json
```

There is no runtime fallback or dual write to `committed.json`. A missing or
unavailable database makes `checksum`, publication `diff`, and `commit_snapshot`
fail explicitly; ordinary dataset operations and comparison `diff` remain
independent of snapshot persistence. If a
legacy `tabular_feed/snapshots/<scope>/<profile>/committed.json` exists while the
database has no committed head, publication fails with
`snapshot_dev_data_transition_required`. Operators must inspect and preserve the
old pointer and artifacts, then explicitly choose a development-data transition;
the runtime neither imports that pointer nor resets or deletes existing data.

The existing variable input maps the original source-byte `raw_sha256` into
checksum and publication diff. Enrichment and final export must not replace it
with the output file's hash: the next checksum compares the same original source
against committed metadata. The existing upstream picker exposes typed result
members, including source `write.sha256` and final `diff.snapshot_artifact_id`;
snapshot revision bookkeeping remains internal.

A candidate represents the full feed, including carried `enriched_*` values.
After selective enrichment, build a final candidate against the original
canonical baseline and reference. Export it through `read(final candidate)` then
`write`; after the destination confirms success, commit that same candidate.
A commit conflict or unknown database outcome does not undo a destination effect
and must not automatically resend it. A safe commit retry checks the durable
receipt. See the [supplier feed sequence](../../user/workflows.md#supplier-feed-and-snapshot-sequence)
for enrichment and export details.

## Consequences

- The tabular engine lives in `core/application/tabular_feed/` and
  `core/infrastructure/tabular_feed/`; it is not an installed extension.
- Runtime execution must not call connector dispatch, extension registry lookup,
  extension actions, subprocess execution, or extension grants for
  `tabular_feed`.
- Builder inspect uses an authenticated core API that can execute only bounded
  inspect work and returns at most ten preview rows.
- The historical extension ID remains only in this ADR's context; runtime and
  upgrade code do not recognize it.
- Generic SDK storage primitives such as `stream`, `stat`, and `put_stream`
  remain available for other extensions.
