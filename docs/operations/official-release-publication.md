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

The namespaces are operator configuration. Either may be omitted for an idempotent
mirror retry, but at least one complete registry must verify before the GitHub
Release is published.

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
   after verification reports at least one complete registry. An incomplete mirror
   can be retried by configuring only that namespace; artifacts are copied again,
   never rebuilt.

GitHub Actions are disabled. Build and validate release artifacts locally in
Docker, then publish the exact validated candidate through the release procedure.
A GitHub Actions artifact is not required.

Verification requires exactly `linux/amd64` and `linux/arm64` for every canonical
release role, including `postgres`, and checks the role and full source-revision
OCI labels on both child configs. Failed registries are recorded independently; artifacts are not deleted or
rolled back automatically.
