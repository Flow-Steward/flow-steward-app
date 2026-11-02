# ADR 0048: Documentation and tag-release Actions

Status: Accepted (owner approved the design on 2026-11-01).

Contributor verification location and timing are amended by
[ADR 0053](0053-hosted-quality-gates.md). Documentation and publication contracts remain.

## Context

Public documentation is maintained in a separate repository and can lag behind
product changes. Registry publication already supports native OCI artifacts, but
requires manual orchestration. Disabling every GitHub Actions workflow prevents
automating these two tasks.

## Decision

Enable Actions for public-source documentation proposals, documentation validation
and deployment, and official stable tag publication. The source is exclusively
`Flow-Steward/flow-steward-app`; the site is
`Flow-Steward/documentation-site`. Private development history is not an input.

AI prepares a bounded proposal for existing documentation pages. A separate
trusted job validates the allowed paths and opens or updates a PR using a narrowly
scoped GitHub App token. AI has no GitHub write or registry credentials. Changes
are built without secrets and require human acceptance before deployment to the existing server at `docs.flowsteward.org`. The accepted source checkpoint travels in the reviewed PR. Bootstrap
checks existing gaps; failures and no-ops do not silently mark changes accepted.

Official stable `vX.Y.Z` tags build once per native Linux AMD64/ARM64 architecture.
The existing role catalog and OCI release scripts remain authoritative. Jobs copy
retained artifacts to both configured registries, verify source labels and child
digests, and require both complete registries before creating a GitHub Release.
Partial failures preserve artifacts/evidence for idempotent mirror retries.
Existing differing version artifacts may not be overwritten.

Product verification continues through `lint.sh`, its single check registry and
disposable `fs-audit` containers. The release workflow runs the full gate for the
exact tagged SHA. Local contributor verification and handoff requirements remain;
remote Actions status is not a substitute for local evidence.

## Consequences

Automation requires scoped GitHub App credentials, an OpenAI API project key,
Docker Hub credentials, GHCR package write access and native Linux runners.
Generated documentation remains a proposal, not proof of product behavior.
Hosting/DNS migration, automatic merging and production application deployment
are outside this decision. Workflow/CLI versions are pinned and must be updated
through review. Missing credentials or failed verification must remain visible.
