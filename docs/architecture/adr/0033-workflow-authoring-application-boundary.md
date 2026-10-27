# ADR 0033: Workflow Authoring Application Boundary

- Status: Accepted
- Date: 2026-10-07

Status note (2026-10-26): the 2026-10-12 amendment below is in turn amended by
[ADR 0044](0044-browser-local-workflow-edit-session.md) — besides
`apply_workflow_changes`, `replace_workflow_content` (edit-session apply) and
`restore_workflow_snapshot` replace the whole definition through the same
judge-and-write path.

## Context

External developer agents need to discover local workflow components, inspect
contracts, validate a bundle and create a disabled draft. Flow Steward already
owns catalog visibility, workflow knowledge, operation contracts, portability,
validation and persistence services. Exposing those mechanisms must preserve the
authenticated MCP/API boundary and the existing execution runtime ownership.

## Decision

`core/application/workflows/authoring/` owns the shared authoring facade and its
catalog, schema and draft behavior. Thin HTTP adapters under
`core/entrypoints/api/` dispatch to it. The MCP authoring tools under
`core/entrypoints/mcp/` map arguments and responses and forward the caller's
identity through the canonical API client. Application services own stores,
searches and workflow persistence; the external agent owns planning.

The surface consists of `capability_search`, `capability_describe`,
`workflow_examples_search`, `workflow_authoring_schema`,
`workflow_draft_validate` and `workflow_draft_save`. It reuses existing scoped
catalogs, knowledge synchronization, persisted step contracts and portability
validators. `core/application/workflows/discovery/` owns deterministic scoped
catalog search, graph/vector retrieval and capability contracts. Knowledge
synchronization lives in `core/application/workflows/workflow_knowledge_sync.py`;
ordinary orchestration shares `contract_sanitation.py`. Discovery, validation
and persistence use deterministic application contracts.
Ordinary workflow revisions remain supported. [ADR 0034](0034-mcp-operations-and-shared-service-boundaries.md)
describes the operational query, report and credential boundaries shared by transports.

Validation and save share preparation for the
`platform.workflow_bundle.v2` format: exactly one explicit workflow ID, existing
dependencies and only `use_existing` or `map_existing` resolutions. Validation
is non-mutating. Save revalidates and atomically creates a disabled workflow,
its project association and normal history; conflicts never overwrite an
existing workflow. It does not install dependencies or create connections.

API role and account/project/workflow policy remain authoritative. Read tools,
including POST validation, use existing read authority; creation requires
existing administrator authority. A launch grant is not a creation grant.
Current machine-token operator policy is retained. Request-local MCP tool
discovery supplements authorization on each call.

The portable plugin in `docs/agent-skills/flow-steward/` packages two namespaced
skills for local reuse/authoring and operational reads/reports through existing
MCP tools, and extension development through the existing public CLI. They
reference current contracts and canonical standards, without copied
catalogs, client-specific engineering rules or a new distribution service.

### Amendment (2026-10-12): one writer of the definition

[ADR 0035](0035-workflow-steps-without-phases.md) makes
`apply_workflow_changes` the only writer of steps, groups and the execution
graph, and `create_workflow_with_definition` the only way to create a workflow
together with its definition. Draft save creates through the latter, so it
stays a create-only path and never edits an existing workflow. Changing an
existing workflow from an external agent reads it with `workflow_definition`
(`GET /api/workflows/{workflow_id}`: steps, groups, graph and revision) and
changes it with `workflow_apply_changes`, which forwards to
`POST /api/workflows/{workflow_id}/changes` with the caller's identity: one
change set, one revision, a `409` on a stale `expected_revision`, and per-operation
reference authorization. It requires the same administrator authority (`admin:all`)
as draft creation. Bundles are `platform.workflow_bundle.v2`; a v1 bundle is
refused before any write.

## Consequences

- Suitable local workflows can be reused without creating duplicate drafts.
- Visual editing, import and external authoring share platform validation and persistence;
  no new workflow dialect or runtime responsibility is introduced.
- Validation reports readiness and unperformed execution checks separately.
  Existing editor Test Run requires enabling a draft when appropriate and does
  not guarantee blanket suppression of external mutations.
- CLI extension lifecycle, client installation, runtime execution and publication
  remain separate operations under their existing permissions.
- Marketplace acquisition, cloud-client setup, extra
  execution tools and new authorization mechanisms require separate scope.

## Related Standards and Decisions

- [Architecture standard](../../standards/architecture-standard.md)
- [Security standard](../../standards/security-standard.md)
- [ADR 0002: Runtime Ownership](0002-runtime-ownership.md)
- [ADR 0015: Atomic Extension Package and Artifact Lifecycle](0015-atomic-extension-package-and-artifact-lifecycle.md)
- [Developer Toolkit guide](../../GUIDE.md#developer-toolkit-local-agent-skills)
