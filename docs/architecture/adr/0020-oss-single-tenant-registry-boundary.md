# ADR 0020: OSS Single-Tenant Registry Boundary

## Status

Accepted — 2026-08-02

## Context

The first open-source release is a single-tenant deployment. Its canonical
`registry_fields` and `registry_data_contracts` tables are deployment-global:
they do not carry `account_id`, and their current keys are globally unique.
`registry_workflows` carries ownership metadata, but its generated
`workflow_id`/`workflow_key` remains globally unique and its dependent step,
group, For each, and runtime tables reference that global identifier. Those catalogs feed
published-block and skill input/output contracts, so simply
pretending that their rows are account-scoped would leave direct REST callers
able to read or mutate the same underlying definitions from dormant secondary
accounts.

The release-hardening decision explicitly defers full multi-account
tenant isolation. The OSS build must still fail closed when a secondary account is
created or selected through a dormant account API.

## Decision

1. The authoritative OSS account is `DEFAULT_ACCOUNT_ID`.
2. Deployment-global field and data-contract REST catalogs are reachable only
   from that account. Every list, detail, create, update, patch, toggle, and
   delete handler rejects a non-default account before it obtains the registry
   store.
3. Workflow creation, clone, and import-apply paths are likewise restricted to
   the authoritative account while workflow identity remains globally unique.
   Duplicate workflow identifiers fail explicitly with `409`; they are never
   silently renamed with a numeric suffix.
4. The current global tables and globally unique keys are **not** represented as
   multi-account-safe isolation. This is an intentional first-release OSS
   boundary, not a substitute for row ownership.
5. Internal system projection continues to populate the one deployment catalog
   under the authoritative account. A foreign interactive principal cannot use
   global-row semantics to claim or mutate it.
6. Full account-qualified registry storage is a separate multi-account
   migration and release gate.

## Multi-Account Migration Backlog

Before multi-account registry access can be enabled, a post-1.0 SQL migration
must:

- add non-null `account_id` ownership to `registry_fields` and
  `registry_data_contracts`, backfilling existing rows to the authoritative
  account;
- replace global key uniqueness with account-qualified uniqueness;
- replace global workflow identity and dependent workflow foreign-key contracts
  with account-qualified identity before enabling secondary-account workflow
  creation;
- make published-block and skill contract references enforce same-account
  ownership, not merely globally valid foreign keys;
- account-scope projection, changelog, trash, restore, retention, search, REST,
  MCP, CLI, and background reconciliation paths;
- prove two-account same-key create/read/update/toggle/delete/restore isolation
  against real PostgreSQL, including non-enumerating foreign-ID failures; and
- provide an explicit upgrade and rollback plan without clearing registry or
  customer data.

Until that backlog is complete, enabling non-default-account access to these
catalogs is an architectural violation.

## Consequences

- A dormant foreign-account session receives a controlled denial and never
  reaches the global registry store.
- Existing single-tenant workflows, projections, and globally referenced field
  IDs continue to work without a premature partial multi-tenant schema.
- The OSS release is honest about its boundary: safe for one authoritative
  account, not certified for multi-account tenant isolation.
- Future multi-tenant work must supersede this ADR rather than deleting the
  transport guard without replacing it with database-enforced ownership.
