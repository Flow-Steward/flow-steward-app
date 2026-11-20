# ADR 0074: Automatic release upgrade

- Status: Accepted
- Date: 2026-11-18

## Context

Requiring separate maintenance, backup, migration and reopening commands made
ordinary upgrades depend on copied commands and operator memory. The operator
approved one command, maintenance retained on failure and an explicit backup waiver.

## Decision

The release application workflow owns the complete operation. Before mutation it
pins the reviewed official target and backup policy in a durable owner-only journal.
It enables and drains maintenance, creates and verifies an operation-scoped backup,
then delegates checkout/image activation to the existing installer handoff.

When that handoff reports pending database work, a technical Docker adapter stops
only runtime writers and runs the prepared target product image's canonical database
upgrade. It re-reads the installed configuration and durable environment after image
preparation. Database/storage services and the retained management process stay up.
Compact can observe its volume-owned maintenance marker from a disposable operator
while its application container is stopped. It does not start application writers.

`--skip-db-backup` records an explicit operator waiver, propagated through the public
release and database request models to migration. It waives neither maintenance nor
writer shutdown, session preflight, SQL advisory locking or protected-data checks.
The ordinary confirmation policy still applies; no additional risk-confirm flag is
required. Existing separate operator commands remain available.

Installer and lifecycle exclusion cover backup and activation, with inherited locks
passed into both installer attempts. A lifecycle journal projection blocks premature
manual reopening. Activation completion is durable before the maintenance service's
health/reconciliation gate is allowed to clear maintenance. Failure before successful
reopening leaves maintenance enabled. A failure persisting final completion restores
maintenance before surfacing the error.

Resume keeps the exact target and prior backup decision even after the installed
identity changes. Completed activation is not repeated. Before remaining mutation,
backup evidence must still be available; a missing pre-upgrade backup fails closed
and is never replaced by a post-migration dump. An explicit waiver can be added after
a backup failure only if activation has not started. Dry-run performs no mutation.

## Consequences

Ordinary updates need one command. The durable journal adds recoverable state and
must be retained on failure; deleting pending state is not a recovery procedure.
A backup waiver accepts loss of database rollback protection and is visible in the
review, journal and database upgrade metadata. Automatic orchestration is available
only once the executing CLI includes this workflow; older CLIs retain their existing
manual initial-upgrade instructions.
