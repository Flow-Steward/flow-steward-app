# ADR 0031: Runtime heartbeat monitoring through RabbitMQ and PostgreSQL

## Status

Accepted, 2026-10-01. The project owner confirmed dynamic registration without
a hardcoded monitoring service catalog and the persistent platform installation
identity projection. Heartbeat publishing, ingestion, persistence and read-side
projection are implemented under the owners below. This record does not certify
release acceptance or a deployed candidate.

## Context

Flow Steward supports Compact and Scalable deployment profiles. Docker or
Compose discovery cannot be the common source of product service health:
platform deployments may not expose a Docker daemon, and Compact already owns
internal role, dependency and worker observations. Its aggregate readiness also
does not establish that every worker replica is ready.

Compact worker observations use an inherited descriptor and a supervisor-owned
readiness registry. The predecessor monitoring summary combined application
readiness checks with Docker inventory and lacked a persisted current-status
record for each runtime instance. Scalable's queue state transitions therefore
needed an equivalent reporting sink independent of the Compact descriptor.

## Decision

Reuse existing observation sources and publish versioned, bounded heartbeat
reports through a dedicated installation-scoped RabbitMQ status channel in both
profiles. Compact retains its local supervisor and short local freshness policy;
broker delivery does not become a prerequisite for readiness or supervision.
Scalable uses the same observation semantics without the Compact fixed inventory.
Compact exposes a bounded read-only observation interface with worker
incarnation, sequence and check timestamps, including expired observations.
That interface does not change supervision or public readiness.
Existing composite worker IDs remain queue/lease identities; a deterministic
monitoring UUID mapping does not convert the existing IDs into UUIDs.

Assign ingestion and monitoring-data retention to the existing singleton
`control` runtime. This extends its background responsibility under
[ADR 0002](0002-runtime-ownership.md), without a new service, privileged daemon,
retained CLI container or Docker socket. Keep ingestion active during maintenance.

Application monitoring owns validation, ordering, persistence and projection.
Infrastructure owns broker transport and technical source adapters. Entrypoints
only compose lifecycle and map authorized requests. Store one current PostgreSQL
record per instance observation, with typed identity, timestamps and sequence,
plus a versioned JSONB payload. Optional history records state transitions, not
every unchanged heartbeat, and has bounded retention. This is not a general
settings or arbitrary key-value store.
Current-record cleanup remains enabled when history is disabled, with separate
retention policies. Expected inventory survives observation cleanup.

The admin API reads stored reports immediately; the UI retains ordinary polling.
RabbitMQ is the transport, not the API's latest-value database. Identity and
profile are checked against trusted deployment configuration and broker scope.
Existing Compose installations keep their journal-backed project identity.
Managed platforms use a UUID provisioned once in durable deployment configuration
and supplied as `FS_INSTALLATION_ID`. The canonical lifecycle context resolver
validates that value and rejects missing or conflicting identity instead of
generating an identity on process boot. Restart/upgrade preserves it; a distinct
new installation uses a different identity and broker namespace.

Incarnation and sequence checks protect each observation stream from duplicates
and reordering. The read projection retains stopped or retired observations as
superseded history and excludes them from current instance counts. For an older
stale group, a fresh group of the same role, observation kind and source can
indicate replacement when its start follows the older group's last report.
Reports lacking start times use the fresh same-source observation as replacement
evidence. The singleton receiver channel is superseded by a later receiver start
even while its previous report is fresh.

These are reporting heuristics, not durable lifecycle-owner slot activation or
an authoritative replacement fence. The implementation has no stable-slot
generation table and makes no guarantee that retirement survives observation
cleanup. Dynamic reports alone do not prove exact replica completeness; the
projection reports completeness only when configuration supplies all desired
counts. The subsystem has no hardcoded service-name catalog: trusted publishers
register through the common validated contract, and expected roles/counts come
from the canonical configuration owner.

Separate freshness, lifecycle, readiness and evidenced health. Preserve last
reported values and timestamps when stale; do not count them as currently
healthy. Broker or consumer unavailability is a reporting-channel problem, not
proof that every component failed. Expected enabled roles come from deployment
configuration; dynamic reports alone do not prove exact replica completeness.
Dependencies and ingress use scoped observations where available and otherwise
remain explicitly unobserved.
Health reports retain their evidence source: process self-report, role/dependency
probe, or worker consumer state. Fresh evidenced health is the reported source's
verdict; it does not certify business progress or broader component behavior.

The concrete contract, policy defaults and validation requirements are in
[the runtime monitoring contract](../../contracts/runtime-monitoring.md).

## Alternatives considered

- Docker/Compose discovery or a retained CLI snapshot collector as the common
  source: does not cover platform deployments or Compact's internal roles and
  adds a deployment-specific prerequisite.
- Read broker messages on each API request: mixes message consumption with a
  repeatable read view and does not preserve the latest report for later readers.
- One shared JSON document for all services: couples unrelated replica updates
  and complicates ordering and retention. Independent instance records retain
  JSONB flexibility without a shared write hotspot.
- New monitoring daemon, provider integrations or WebSocket delivery: not
  required for the agreed first implementation.

## Consequences

Monitoring works through the same product contract in both profiles and reuses
available checks. It adds a bounded reporting responsibility to existing
runtimes, a dedicated broker channel and monitoring tables. It requires explicit
source semantics, trusted installation projection, idempotent ingestion and
truthful stale/unavailable rendering.

Reports are eventually consistent and may be unavailable when the reporting
path fails. A background publisher being alive does not prove business progress.
Status reports never acknowledge jobs, free leases, restart roles, alter public
readiness or become a new control protocol. Compact supervision, existing
dependency checks and the database artifact policy in
[ADR 0012](0012-post-1.0-sql-migrations.md) remain authoritative.
