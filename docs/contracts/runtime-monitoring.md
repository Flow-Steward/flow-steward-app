# Runtime monitoring contract

This contract describes the implemented observation pipeline in
[ADR 0031](../architecture/adr/0031-runtime-heartbeat-monitoring.md).
It works with Compact and Scalable without making Docker/container discovery a
product-health dependency.

## Owners and data flow

First-party runtimes publish bounded status observations to an installation/profile
scoped RabbitMQ exchange and durable queue. Control owns subscription, ingestion
and retention, including during maintenance. The application monitoring domain
owns validation, transactional persistence and the read projection. Infrastructure
owns broker transport and role/Compact source adapters.

`GET /api/system/monitoring/summary` reads stored PostgreSQL observations. An HTTP
request never drains RabbitMQ or synchronously refreshes components. UI polling
and manual refresh use this endpoint; no separate WebSocket subscription is
required. Component observations do not restart services, control admission,
complete jobs or change lease/readiness policy.

## Envelope and validation

Version 1 reports contain `schema_version`, trusted `installation_id`,
`deployment_profile`, bounded dynamic `service_type`, UUID `instance_id` and
`incarnation_id`, positive `sequence`, UTC `observed_at`, bounded `source` and
a typed JSON payload. `received_at` is assigned by the database.

The payload type is `runtime_status`, version 1. It separates lifecycle,
readiness, health, consumer state, source availability/age/budget, observation
kind and optional `started_at`. Observation kinds are `process`, `dependency`,
`role_probe` and `channel`. A JSONB storage column is not a generic key-value API:
payloads are validated against this closed contract.

Messages are at most 16 KiB. Duplicate JSON keys, unknown fields, wrong
installation/profile, expired reports and timestamps more than 30 seconds in the
future are rejected. Service names are dynamic, not a hardcoded service enum.
Opaque worker identity maps deterministically to a monitoring UUID without
changing job/lease identity. Installation identity is persisted by the canonical
configuration/lifecycle owner and isolated by clone procedures.

## Persistence and delivery

`public.fs_runtime_status_current` stores one record per
`(installation_id, service_type, instance_id, incarnation_id, source)`.
Ingestion conditionally accepts only newer sequences in a transaction.
Duplicate/reordered messages cannot refresh receipt time or create history.
Acknowledgement follows commit; commit-before-ack redelivery is harmless.
Invalid messages are rejected, not infinitely requeued. Database/transport errors
use bounded retry/reconnection; an outage is not acknowledged as successful storage.

Optional `public.fs_runtime_status_history` records normalized state changes,
not every timestamp/sequence update. Its insert shares the current-row transaction.
Cleanup is bounded and independent of whether history recording is enabled.
Expected inventory comes from canonical configuration, not retained rows.

Broker connections and timeouts are independent of job-consumer channels. Queue
size and TTL are finite. Reconnecting publication must preserve source-check
provenance, not disguise an old check with a fresh publication timestamp.
The receiver writes its own channel observation directly, so broker failure can
be reported without requiring publication through the failed broker.

## Freshness, health and counts

Default reporting interval is 60 seconds and the report stale threshold is
180 seconds. Report and receipt ages are both visible; effective report age is
their greater nonnegative age. Compact source/check budgets apply at publication;
after publication the report budget applies. An already expired source check
never becomes fresh simply because its report arrived recently.

Readiness and health require their own timestamp and evidence. A dependency or
role probe derives health from its check; worker health may rest on consumer
state, and a reporting process on `self_report` liveness. The evidence identifies
the strength of that claim: liveness is not proof of application progress.
Missing evidence is `not_observed`, never silently healthy.

Rows expose original facts, age, freshness, conflicts and supersession. Stopped or
retired observations are historical. The current read projection also identifies
replacement from the same role/source's start-time evidence and excludes
superseded observations from current counts. This is a diagnostic projection,
not a durable authoritative lifecycle generation/fence. Unknown desired replica
counts do not establish exact completeness.

Counts distinguish roles, current instances, fresh/stale, ready and evidenced
healthy instances. Multiple observer sources do not count as multiple process
instances. Dependency and channel observations do not inflate process counts.
Conflicting current observations are visible and cannot be silently resolved to
green. Expected-role gaps distinguish missing/stale or unobserved replica target;
disabled optional roles are not missing. Transient CLI/audit/test containers are
not product services.

Channel unavailable/stale is distinct from a proven service failure. Persisted
rows retain normal age evaluation after restart. Existing readiness checks and
authorization remain separate contracts; a produced summary may be HTTP 200
while reporting failing checks or stale components.

## Configuration and security

Canonical settings and all bounds live in
`core/infrastructure/config/monitoring_runtime.py`; their generated descriptions
are in the [environment catalog](../configuration/flow-steward-env-catalog.md).
Defaults: history disabled, current/history retention seven days, cleanup batches
500 rows, queue 10,000 messages/16 MiB, operation timeout two seconds. Reporting
interval must be less than the stale threshold.

Monitoring resources are scoped to installation/profile and carry no job-control
semantics. Observations come from trusted first-party runtimes, not cryptographic
proof against a compromised service. Secrets and extension output must not be
included. Extension subprocesses do not receive monitoring-channel credentials.

Source owners are `core/application/monitoring/{contracts,ingestion,repository,queries}.py`,
`core/infrastructure/runtime/monitoring/` and the canonical monitoring configuration.
Database artifacts follow [ADR 0012](../architecture/adr/0012-post-1.0-sql-migrations.md).
