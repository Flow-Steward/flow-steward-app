# ADR 0048: Documentation and tag-release Actions

Status: Accepted (owner approved the design on 2026-11-01).

Contributor verification location and timing are amended by
[ADR 0053](0053-hosted-quality-gates.md). Exact-SHA publication gate reuse is
amended by [ADR 0054](0054-reuse-exact-sha-release-gate.md).

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
retained artifacts to both configured registries and verify source labels and child
digests. The owner amended the availability rule on 2026-11-03: one complete
verified registry is sufficient to create the GitHub Release, so a failed mirror
does not disable registry redundancy. A complete registry contains the entire
canonical role set and both native platforms; partial registries are never mixed.
Image-identity failures, immutable-version conflicts and disagreement between
verified child digests remain fatal. Release notes and evidence identify unavailable
mirrors, and preserve artifacts for idempotent retries.
Existing differing version artifacts may not be overwritten.

Product verification continues through `lint.sh`, its single check registry and
disposable `fs-audit` containers. The release workflow runs the full gate for the
exact tagged SHA or reuses a verified prior exact-SHA hosted gate per ADR 0054.
Local contributor verification and handoff requirements remain;
remote Actions status is not a substitute for local evidence.

## Consequences

Automation requires scoped GitHub App credentials, an OpenAI API project key,
Docker Hub credentials, GHCR package write access and native Linux runners.
Generated documentation remains a proposal, not proof of product behavior.
Hosting/DNS migration, automatic merging and production application deployment
are outside this decision. Workflow/CLI versions are pinned and must be updated
through review. Missing credentials or failed verification must remain visible.
