# ADR 0034: MCP Operations and Shared Service Boundaries

- Status: Accepted
- Date: 2026-10-07

## Context

Business owners need to find failed or blocked runs, review pending approvals,
inspect outcomes and request operational reports through their preferred agent.
These operations share application services across the API, CLI and MCP transports.

## Decision

Job query/command and reporting services stay in their owning application domains.
HTTP, CLI and MCP entrypoints are thin adapters. MCP forwards the original caller's
credentials to the canonical API; it does not own queries, persistence, permissions
or planning. Request-local discovery supplements authorization on each call.

`job_list` exposes scoped pagination and attention grouping; `job_approvals_pending`
exposes approvals without requiring a known job ID. Query failures remain errors.
`report_generate` accepts `params.workflow_id` for `activity_daily`,
`project_summary` and `project_stats`, alongside project/date filters.
Application-owned predicates filter records before pagination, counting or aggregation.
Account-wide rollups lacking tenant/project/workflow/workspace dimensions cannot
establish authority for scoped operational reports. Persisted reports enforce the
reader's current authority and their recorded content scope. Job report authorization
does not load logs or invoke runtime work.

Workflow runtime messages, approval threads, execution records, checkpoints and
provider/runtime memory retain their application owners. Ordinary workflow revisions
use existing history and persistence services. Shared authoring follows ADR 0033.

`core/application/settings/embedding_credentials.py` and `embedding_repository.py`
resolve an explicit account-scoped embedding provider independently of transport.
The repository reads the `provider` field only from the account-scoped
`embedding_settings` JSON setting, with an explicit `custom:<named_provider_id>`
selection. Fresh schema and seed data contain no selections, so embeddings remain
unconfigured until a provider is explicitly selected through internal account
settings storage. The service uses the verified, encrypted named-provider resolver
and returns no credential for missing, invalid,
unavailable, foreign-account or non-OpenAI selections. It never selects an arbitrary
provider or falls back to ambient credentials. Project and agent model routing keep
their current verified provider boundaries.

## Consequences

- Transports reuse the same application-owned query, report and authoring services.
- Operators can discover pending approvals and scoped job outcomes through MCP.
- Domain policy and repositories stay in `core/application`.
- Execution, approval and publication remain deliberate operations under existing authority.
- Credential storage, provider verification and runtime routing retain their established security rules.

## Related Decisions

- [ADR 0018](0018-verified-llm-routing-and-extension-boundaries.md)
- [ADR 0033](0033-workflow-authoring-application-boundary.md)
- [Architecture standard](../../standards/architecture-standard.md)
- [Security standard](../../standards/security-standard.md)
