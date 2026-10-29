# Architecture Decision Records

This directory stores the canonical architecture decision records for the Flow Steward repository.

## Purpose

ADRs record durable architectural decisions, approved exceptions, and the reasons behind them.
They complement `docs/standards/*` by explaining why a rule or structural choice exists.

## When an ADR Is Required

Create or update an ADR for:

- a new architectural pattern
- a new runtime responsibility
- a significant cross-domain dependency
- a new shared abstraction used by multiple domains
- a significant security architecture decision
- a frontend stack decision
- a migration strategy decision
- an approved exception to a `MUST` rule in the standards

## Status Values

- `Proposed`
- `Accepted`
- `Superseded`
- `Deprecated`

Bootstrap ADRs start as `Proposed` until the project owner explicitly ratifies
them. Do not mark a bootstrap ADR `Accepted` by default just because it was
written as part of standards-extension setup.

## Naming

Use zero-padded numeric prefixes, for example `0001-system-style.md`. A new
ADR takes the next unused number; never reuse a retired number.

## Update Rules

- Update the existing ADR when clarifying its consequences without changing the decision.
- Create a new ADR when replacing a prior decision.
- Mark the old ADR as `Superseded` and link to the new one when a decision changes.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-system-style.md) | System Style | Proposed |
| [0002](0002-runtime-ownership.md) | Runtime Ownership | Proposed |
| [0003](0003-service-layer.md) | Service Layer | Proposed |
| [0004](0004-domain-organization.md) | Domain Organization | Proposed |
| [0005](0005-testing-policy.md) | Testing Policy | Proposed |
| [0006](0006-frontend-stack.md) | Frontend Stack | Superseded by 0008 |
| [0007](0007-repository-structure.md) | Canonical Repository Structure | Proposed |
| [0008](0008-react-fastapi-frontend-migration.md) | React + FastAPI Frontend Migration | Accepted |
| [0009](0009-canonical-agent-memory-platform.md) | Canonical Agent Memory Platform | Accepted |
| [0010](0010-flowsteward-builder-test-run-rule.md) | FlowSteward Builder Test Run Rule | Accepted |
| [0011](0011-release-upgrade-path.md) | Canonical Release Upgrade Path | Superseded by 0012 |
| [0012](0012-post-1.0-sql-migrations.md) | Post-1.0 SQL Migrations on a Schema Baseline | Accepted |
| [0013](0013-extension-package-identity-and-dependencies.md) | Extension Package Identity And Dependencies | Accepted |
| [0014](0014-core-owned-tabular-feed-step-kind.md) | Core-Owned Tabular Feed Step Kind | Accepted |
| [0015](0015-atomic-extension-package-and-artifact-lifecycle.md) | Atomic Extension Package and Artifact Lifecycle | Accepted |
| [0016](0016-host-mediated-extension-data-migrations.md) | Host-Mediated Extension Data Migrations | Accepted |
| [0017](0017-schema-driven-cli-tui-stack.md) | Schema-Driven CLI and Terminal UI Stack | Accepted |
| [0018](0018-verified-llm-routing-and-extension-boundaries.md) | Verified LLM Routing and Extension Boundaries | Accepted |
| [0019](0019-dataset-connection-actions-in-application-layer.md) | Dataset Connection Actions Belong to the Application Layer | Accepted |
| [0020](0020-oss-single-tenant-registry-boundary.md) | OSS Single-Tenant Registry Boundary | Accepted |
| [0021](0021-quality-evidence-trust-boundary.md) | Quality Evidence Trust Boundary | Accepted (amended by 0036) |
| 0022 | Google Workspace OAuth boundary | Retired: superseded by 0023 and removed |
| [0023](0023-extension-owned-oauth-boundary.md) | Extension-Owned OAuth Boundary | Accepted |
| [0024](0024-provider-declared-oauth-protocol-variance.md) | Provider-Declared OAuth Protocol Variance | Proposed (amends 0023) |
| [0025](0025-encrypted-external-effect-replay.md) | Encrypted External-Effect Replay | Proposed |
| [0026](0026-provider-neutral-operation-retry.md) | Provider-Neutral Operation Retry Ownership | Proposed |
| 0027 | Shopify bulk product submission | Retired: moved to its extension |
| [0028](0028-web-extension-installer.md) | Browser access to the isolated extension installer | Accepted |
| [0029](0029-compact-and-scalable-deployment.md) | Compact and Scalable production deployment | Accepted |
| [0030](0030-local-install-and-private-release-auth.md) | Local install identity and private update authentication | Accepted |
| [0031](0031-runtime-heartbeat-monitoring.md) | Runtime heartbeat monitoring through RabbitMQ and PostgreSQL | Accepted |
| [0032](0032-workflow-launch.md) | Explicit workflow launch | Accepted |
| [0033](0033-workflow-authoring-application-boundary.md) | Workflow Authoring Application Boundary | Accepted (amended by 0044) |
| [0034](0034-mcp-operations-and-shared-service-boundaries.md) | MCP Operations and Shared Service Boundaries | Accepted |
| [0035](0035-workflow-steps-without-phases.md) | Workflow Steps Without Mandatory Phases | Accepted (amended by 0038) |
| [0036](0036-content-scoped-quality-evidence.md) | Content-Scoped Quality Evidence and Shared Gate Caches | Accepted |
| [0037](0037-parallel-backend-unit-suite.md) | Parallel Backend Unit Suite with Per-Worker Audit Databases | Accepted |
| [0038](0038-workflow-containers-for-each-collect-hooks.md) | Run-Once Groups, a Standalone For Each, Collect and Hooks | Accepted |
| [0039](0039-workflow-files-returned-from-steps.md) | Files Returned From Steps, Not Declared in the Interface | Accepted |
| [0040](0040-attempt-output-contract-receipts.md) | Execution-attempt output contracts and internal producer receipts | Accepted |
| [0041](0041-workflow-trigger-retirement.md) | Workflow trigger retirement and current-reference credential decisions | Accepted |
| [0042](0042-bounded-state-selector-shape-proof.md) | Bounded state selector shape proof | Accepted |
| [0043](0043-rabbitmq-transport-trust-boundary.md) | RabbitMQ transport trust boundary | Accepted |
| [0044](0044-browser-local-workflow-edit-session.md) | Browser-Local Workflow Edit Session and One Whole-Content Writer | Accepted |
| [0045](0045-terminal-artifact-presentation.md) | Terminal presentation claims for prepared extension artifacts | Accepted |
| [0046](0046-projected-skill-identity-anchors.md) | Identity anchors for installed projected skills | Accepted |

ADR 0043 (RabbitMQ transport) and ADR 0044 (browser-local edit session) were
first filed as second 0036 and 0039 documents and renumbered on 2026-10-26;
an older note that says "ADR 0036" about the RabbitMQ transport, or "ADR 0039"
about the browser edit session, means these.
