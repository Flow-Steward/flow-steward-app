# Official release publication

Official production releases are published from an immutable `vX.Y.Z` tag whose
commit contains the same `X.Y.Z` in `VERSION`. Product images and the canonical
PostgreSQL image (`infra/docker/postgres-extended/Dockerfile`, built with the same
`AGE_REF` as every Compose file) are built once on native AMD64 and ARM64 Linux
hosts, copied to registries without rebuilding, and exposed through one
two-platform index per role. Other third-party infrastructure images (RabbitMQ,
Traefik, SeaweedFS) are not republished.

## Configuration

Authenticate Docker/Skopeo through their credential stores; do not place tokens in
arguments or shell history. Set only non-secret release inputs:

```bash
export FS_RELEASE_VERSION=1.0.0
export FS_RELEASE_REVISION="$(git rev-parse 'refs/tags/v1.0.0^{commit}')"
export FS_RELEASE_ARTIFACT_DIR=/secure/release/1.0.0
export FS_RELEASE_GHCR_NAMESPACE=ghcr.io/flow-steward
export FS_RELEASE_DOCKERHUB_NAMESPACE=docker.io/flowsteward
```

The namespaces are operator configuration. Automated publication requires both
registries to verify. The lower-level manual scripts may configure one namespace
for an idempotent mirror retry; verify the complete pair before announcing a
new official release.

Each namespace holds seven public repositories, one per release image role
(`core/application/release/images.py`):

| Repository | Role | Used by |
| --- | --- | --- |
| `flow-steward-compact` | `compact-runtime` | Compact |
| `flow-steward-app` | `fs-app` | Scalable |
| `flow-steward-worker` | `fs-worker` | Scalable |
| `flow-steward-control` | `control-plane` | Scalable |
| `flow-steward-cli` | `fs-cli` | Compact and Scalable |
| `flow-steward-mcp` | `fs-mcp` | Compact and Scalable |
| `flow-steward-postgres` | `postgres` | Compact and Scalable (from 0.9.2) |

On GHCR they are packages of the Flow-Steward organization, created by the first
push; make each package public and link it to the `Flow-Steward/flow-steward-app`
repository. On Docker Hub, create the seven repositories under `flowsteward` as
public before the first push, or check that the first push created them public.
`flow-steward-postgres` is new in 0.9.2: create it (Docker Hub) or make its package
public (GHCR) before publishing that release.

Releases 0.9.0 and 0.9.1 were published without `flow-steward-postgres`, and their
resolution never asks for it. From 0.9.2 a registry lacking it holds an incomplete
release set. An installation whose release identity names no PostgreSQL image keeps
its own, built locally when absent (ADR 0030).

Each native host must select a local Buildx `docker-container` builder attached to
the active Docker context. The Docker daemon server architecture must match
`FS_RELEASE_PLATFORM`; the build command refuses remote, emulated, and legacy
`docker`-driver builders because the release output is an OCI archive.

## Sequence

1. Create and push the immutable tag only after the full release gate passes.
2. On native AMD64, set `FS_RELEASE_PLATFORM=linux/amd64` and run
   `scripts/release/official-release-build.sh`.
3. On native ARM64, use the same tag, revision, and artifact directory, set
   `FS_RELEASE_PLATFORM=linux/arm64`, and run the same build script.
4. Collect both native artifact sets without changing their bytes. From the
   publication host, run `scripts/release/official-release-publish.sh`.
5. Set `FS_RELEASE_EVIDENCE` to an owner-controlled JSON path and run
   `scripts/release/official-release-verify.sh`. Retain the resulting per-registry
   index-digest evidence with the release record.
6. Publish the stable, non-draft, non-prerelease GitHub Release for `vX.Y.Z` only
   after verification reports both configured registries complete. An incomplete mirror
   can be retried by configuring only that namespace; artifacts are copied again,
   never rebuilt.

GitHub Actions orchestrates the same native build and publication scripts as
described below. Local contributor gates remain required; automated publication
also runs the canonical full Docker gate on the exact tagged commit (ADR 0048).

Verification requires exactly `linux/amd64` and `linux/arm64` for every canonical
release role, including `postgres`, and checks the role and full source-revision
OCI labels on both child configs. Failed registries are recorded independently; artifacts are not deleted or
rolled back automatically.


## GitHub Actions setup

The workflow is `.github/workflows/release-images.yml` in
`Flow-Steward/flow-steward-app`. Enable Actions, retain the default read-only token
policy, and allow the workflow's publication job to request Contents: write and
Packages: write. Ensure organization package settings allow publication from this
repository. Protect stable tags against replacement/deletion through repository
rules. Do not create a tag during setup just to exercise the pipeline.

Set repository variables:

- `RELEASE_GHCR_NAMESPACE`: for example `ghcr.io/flow-steward`.
- `RELEASE_DOCKERHUB_NAMESPACE`: for example `docker.io/flowsteward`.
- `DOCKERHUB_USERNAME`: the account authorized to push to the configured namespace.
- Optional `RELEASE_GATE_RUNNER`: a dedicated Linux runner label if the default
  `ubuntu-24.04` runner lacks resources for the complete Docker gate.

Store `DOCKERHUB_TOKEN` as a repository secret with write access to all release
repositories. GHCR uses the job's scoped `GITHUB_TOKEN`; an existing package must
allow this repository write access. Configure secrets in GitHub settings, never
commit them or paste them into logs/command arguments. Native builds use
`ubuntu-24.04` and `ubuntu-24.04-arm`; both must be available to the organization.
The full gate builds its PostgreSQL dependency and audit image on a dedicated
runner, with the repository's development example environment and disposable audit
DB/broker. It does not connect to a production installation.

After local release verification and a reviewed VERSION change, push the matching
stable tag `vX.Y.Z`. The workflow validates the tag, VERSION, clean source and main
ancestry, runs complete Tier 2 + Tier 3 checks, builds each native role set and
retains OCI archives for 30 days. It validates archive checksums before accessing
registries and rejects any existing version whose child/index digests differ.
No latest aliases or prerelease images are produced.

Publication attempts each mirror independently. Both registries must expose every
catalog role and both architectures with matching child digests and source labels.
Only then is a stable GitHub Release created, with registry verification JSON
attached. Registry evidence is also retained as a workflow artifact for 90 days;
quality evidence and native archives retain for 30 days. Configure organization
artifact limits to permit these durations and sufficient archive storage.

## Retry without rebuilding

For a native build failure, use GitHub's **Re-run failed jobs**, so only the failed
architecture is built. For publication failures after both native archives exist,
re-run failed jobs, or run **Publish official images** manually from main with:

- `tag`: the original immutable tag;
- `artifact_run_id`: the numeric original workflow run ID holding both native
  artifacts (visible in its Actions URL).

Manual publication retry verifies that the artifacts came from this repository's
release workflow, checks source/version/architecture metadata and every archive
checksum, runs the exact-SHA quality gate again, then copies the same bytes. A
full re-run of an existing run also selects its retained native artifacts. If an
archive is missing/expired, the workflow fails; it never silently rebuilds under
publication-retry mode. A fresh build attempt must pass immutability checks and
cannot replace already-published differing content. Preserve the original archives
outside Actions if publication may be delayed beyond retention.

A partial mirror is a failed automated release even though the low-level verifier
can produce useful evidence for the one healthy registry. Retrying an already
complete mirror is safe only with identical archive digests. The workflow does not
delete partial tags or roll back registry content. No production application
rollout is part of this workflow.

## Documentation automation

`.github/workflows/docs-sync.yml` dispatches public main changes to
`Flow-Steward/documentation-site`. Install a GitHub App on that documentation
repository with Contents: write and Pull requests: write. Set
`DOCS_SYNC_APP_ID` (variable) and `DOCS_SYNC_APP_PRIVATE_KEY` (secret) in both
repositories. The source workflow requests only the token's Contents: write
permission. No private development checkout is read.

In the documentation repository, set `DOCS_SYNC_ACTOR` to the App's exact bot login,
`OPENAI_API_KEY` as a secret and optionally `DOCS_AI_MODEL` as a variable. Run its
**Propose product documentation updates** workflow manually for the initial full
reconciliation. Subsequent runs inspect all changes since the last accepted source
checkpoint and update one bot PR. Review and merge it to publish through the
existing Pages workflow. Configure Pages' actual origin/base path before overriding
`SITE_URL`/`BASE_URL`; the automation preserves the existing Pages target by default.
See the documentation repository README for review, failure and retry behavior.
