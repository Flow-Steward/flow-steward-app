# Official release publication

Official production releases are published from an immutable `vX.Y.Z` tag whose
commit contains the same `X.Y.Z` in `VERSION`. Product images, the installer, and
the canonical PostgreSQL image (`infra/docker/postgres-extended/Dockerfile`, built
with the same `AGE_REF` as every Compose file) are built once on native AMD64 and
ARM64 Linux hosts, copied to registries without rebuilding, and exposed through one
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

The namespaces are operator configuration and redundant publication targets. Automated
publication requires at least one complete verified registry. A healthy registry must
contain every canonical role and both native architectures; roles from different
partial mirrors are never combined. An unavailable mirror is reported in release
notes and retained evidence without blocking the healthy registry. Invalid image
identity, conflicting immutable tags, and cross-registry digest mismatches remain
fatal. The lower-level scripts may configure one namespace for an idempotent retry.

Each namespace holds eight public repositories, listed in the publication catalog
`core/application/release/images.py`. The installer is a bootstrap artifact; the
other seven roles are deployment images:

| Repository | Role | Used by |
| --- | --- | --- |
| `flow-steward-compact` | `compact-runtime` | Compact |
| `flow-steward-app` | `fs-app` | Scalable |
| `flow-steward-worker` | `fs-worker` | Scalable |
| `flow-steward-control` | `control-plane` | Scalable |
| `flow-steward-cli` | `fs-cli` | Compact and Scalable |
| `flow-steward-mcp` | `fs-mcp` | Compact and Scalable |
| `flow-steward-postgres` | `postgres` | Compact and Scalable (from 0.9.2) |
| `flow-steward-installer` | `installer` | Installation bootstrap |

On GHCR they are packages of the Flow-Steward organization, created by the first
push; make each package public and link it to the `Flow-Steward/flow-steward-app`
repository. On Docker Hub, create the eight repositories under `flowsteward` as
public before the first push, or check that the first push created them public.
`flow-steward-postgres` is new in 0.9.2: create it (Docker Hub) or make its package
public (GHCR) before publishing that release.

The new installer requires explicit visibility setup: after the first push, set
`ghcr.io/flow-steward/flow-steward-installer` to **Public** in its package settings;
create `flowsteward/flow-steward-installer` as a **Public** Docker Hub repository
or confirm that visibility after creation. A public source repository does not
prove public package visibility. [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#pushing-container-images)
states that newly published packages initially have private visibility.

Releases 0.9.0 and 0.9.1 were published without `flow-steward-postgres`, and their
resolution never asks for it. From 0.9.2 a registry lacking it holds an incomplete
release set. An installation whose release identity names no PostgreSQL image keeps
its own, built locally when absent (ADR 0030).

The installer uses the same `X.Y.Z` image tag convention as the release images,
with native child tags `X.Y.Z-amd64` and `X.Y.Z-arm64`. The default bootstrap
pulls the versioned installer from the official registries; missing published
images fail installation without a local source-build fallback. Explicit source
installation remains a separate operator choice.

The installer is a publication-only artifact. It is required by native build
metadata, mirroring, index creation, and publication verification, but does not
join the durable release identity or the deployment role sets. Collect
`installer-amd64.oci.tar` and `installer-arm64.oci.tar` with the other archives
and checksummed `build-amd64.json` and `build-arm64.json` records. Mirror retries
reuse these same archives.

Each native host must select a local Buildx `docker-container` builder attached to
the active Docker context. The Docker daemon server architecture must match
`FS_RELEASE_PLATFORM`; the build command refuses remote, emulated, and legacy
`docker`-driver builders because the release output is an OCI archive.

## Prepare product release notes before tagging

Follow the canonical
[Product Release Notes standard](../standards/agent-workflow-standard.md#product-release-notes)
and [authoring checklist](../releases/README.md). Commit
`docs/releases/<version>.md` with the matching `VERSION` change before creating
the immutable tag. Describe shipped features, improvements and fixes, plus any
compatibility or upgrade actions. Review the complete comparison from the
previous published stable release, not only the release branch's own commits.

The resolve job validates the versioned file against published release metadata
before either native build starts. Missing files, empty or placeholder sections,
wrong versions and incorrect changelog links fail closed. Publication rechecks
the file digest against the resolve result.

New releases use these reviewed notes. Retries preserve valid human edits and
repair a registry-only description from the source notes. Registry availability
is appended as a footer; technical provenance does not replace product notes or
imply that unexecuted tests passed.

## Sequence

1. Complete selected verification and any separately requested full acceptance,
   commit the reviewed versioned notes, then create and push the immutable tag.
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
   after authenticated digest verification and the anonymous installer acceptance
   below establish at least one complete publicly accessible registry. Check both
   configured namespaces and report an unavailable mirror explicitly. An unavailable mirror
   can be retried by configuring only that namespace; artifacts are copied again,
   never rebuilt.

GitHub Actions orchestrates the same native build and publication scripts as
described below. Local contributor gates remain required. Ordinary automated
publication validates the notes, exact source identity, native archives and
registry evidence; it does not start a full quality run. If complete exact-SHA
quality evidence is explicitly required, obtain it through a separately requested
run before publication.

Verification requires exactly `linux/amd64` and `linux/arm64` for every canonical
release role, including `postgres`, plus the installer artifact, and checks the
role and full source-revision OCI labels on both child configs. An otherwise
complete registry without its installer is incomplete. Failed registries are
recorded independently; artifacts are not deleted or rolled back automatically.


## Anonymous installer acceptance before stable release

Authenticated `official-release-verify.sh` checks artifact bytes, platform configs,
and labels using the publisher's credentials. It does not prove that the default
anonymous bootstrap can download the installer. Before publishing the stable
GitHub Release, check anonymous manifest access and actual image pulls for
`linux/amd64` and `linux/arm64` in both configured registries. Do not advertise a
mirror as available until both platform checks succeed there.

Run the following block separately for each namespace, using a newly created,
owned disposable acceptance daemon with an empty image store for each registry.
Record that daemon's ownership and endpoint first, and select it through
`DOCKER_HOST`; do not use a production daemon. This prevents cached layers from
masking a failed anonymous download. Use Docker API 1.49 or later for explicit
platform inspection. These pulls do not execute the installer and require no
CPU emulation. The temporary client configuration starts without credentials;
its isolated `PATH` contains only the Docker CLI so credential helpers cannot be
auto-discovered. No login is performed.

```bash
# Run once with each value, against a fresh owned acceptance daemon each time:
installer_namespace=ghcr.io/flow-steward
# installer_namespace=docker.io/flowsteward
(
  set -eu
  : "${FS_RELEASE_VERSION:?set exact X.Y.Z}"
  : "${FS_RELEASE_REVISION:?set exact source commit}"
  : "${FS_RELEASE_ARTIFACT_DIR:?set retained release evidence directory}"
  : "${DOCKER_HOST:?select the owned empty acceptance daemon}"
  installer_docker_cli=$(command -v docker)
  installer_anon_config=$(mktemp -d)
  installer_original_path=$PATH
  trap 'PATH=$installer_original_path; export PATH; rm -rf "$installer_anon_config"' EXIT
  mkdir "$installer_anon_config/bin"
  ln -s "$installer_docker_cli" "$installer_anon_config/bin/docker"
  printf '%s\n' '{"auths":{},"credHelpers":{}}' > "$installer_anon_config/config.json"
  installer_anon_evidence="$FS_RELEASE_ARTIFACT_DIR/anonymous-installer/$installer_namespace"
  mkdir -p "$installer_anon_evidence"
  export DOCKER_CONFIG="$installer_anon_config"
  unset DOCKER_CONTEXT DOCKER_AUTH_CONFIG
  export PATH="$installer_anon_config/bin"
  installer_reference="$installer_namespace/flow-steward-installer:$FS_RELEASE_VERSION"
  docker manifest inspect --verbose "$installer_reference" \
    > "$installer_anon_evidence/manifest.json"
  for installer_architecture in amd64 arm64; do
    installer_platform="linux/$installer_architecture"
    docker pull --platform "$installer_platform" "$installer_reference" \
      > "$installer_anon_evidence/pull-$installer_architecture.log" 2>&1
    docker image inspect --platform "$installer_platform" "$installer_reference" \
      > "$installer_anon_evidence/image-$installer_architecture.json"
    test "$(docker image inspect --platform "$installer_platform" \
      --format '{{.Os}}/{{.Architecture}}' "$installer_reference")" = "$installer_platform"
    test "$(docker image inspect --platform "$installer_platform" \
      --format '{{index .Config.Labels "org.opencontainers.image.role"}}' \
      "$installer_reference")" = installer
    test "$(docker image inspect --platform "$installer_platform" \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$installer_reference")" = "$FS_RELEASE_REVISION"
  done
)
```

Compare each recorded index/child manifest and image config digest with the
retained authenticated verification and native build records for the same source
commit; labels alone are insufficient. Preserve the package reference, `X.Y.Z`
tag, both platform identities, command outcomes and evidence paths in the release
record. See Docker's [pull](https://docs.docker.com/reference/cli/docker/image/pull/),
[manifest inspection](https://docs.docker.com/reference/cli/docker/manifest/inspect/),
and [platform image inspection](https://docs.docker.com/reference/cli/docker/image/inspect/)
references for these commands.

The block removes its temporary client configuration. After preserving evidence,
remove the exact owned acceptance daemon and its disposable image store, along
with any other owned containers, networks or volumes created for these checks.
Retain the minimal identity evidence and logs; do not prune shared Docker resources.
If a command fails, preserve its evidence, mark that registry/platform unavailable
or pending, and follow the mirror reporting policy above.

Until separately authorized publication has created the package tags and these
checks have run, installer package visibility, tag/index/child digests and all four
registry/platform anonymous acceptance results are **pending**. Local source
regressions and authenticated publication verification do not satisfy this check.


## GitHub Actions setup

The workflow is `.github/workflows/release-images.yml` in
`Flow-Steward/flow-steward-app`. Enable Actions, retain the default read-only token
policy, and allow the workflow's publication job to request Contents: write and
Packages: write. Ensure organization package settings allow publication from this
repository. Protect stable tags against replacement/deletion through repository
rules. Do not create a tag during setup just to exercise the pipeline.

The official workflow uses the existing registries and account by default. These
repository variables are optional overrides:

- `RELEASE_GHCR_NAMESPACE`: defaults to `ghcr.io/flow-steward`.
- `RELEASE_DOCKERHUB_NAMESPACE`: defaults to `docker.io/flowsteward`.
- `DOCKERHUB_USERNAME`: defaults to `flowsteward`; an override must authorize
  publication to the configured namespace.
- `RELEASE_GATE_RUNNER`: a GitHub-hosted Linux runner override; the default is
  `ubuntu-24.04`. The hosted quality adapter does not support persistent
  self-hosted runners.

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

Publication attempts each mirror independently. A stable GitHub Release is created
when at least one registry exposes every catalog role, the installer artifact,
and both architectures with the expected native-artifact digests and source labels. If both registries verify,
their child digests must match. The release notes name the available and unavailable
registries; registry verification JSON is attached. Registry evidence is also retained as a workflow artifact for 90 days;
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
checksum, validates the applicable exact-SHA quality evidence, then copies the same bytes.
Publication tooling is pinned to the workflow commit and runs against a separate
clean checkout of the immutable tagged source, so an older tag can use current
recovery tooling without moving its source revision. A
full re-run of an existing run also selects its retained native artifacts. If an
archive is missing/expired, the workflow fails; it never silently rebuilds under
publication-retry mode. A fresh build attempt must pass immutability checks and
cannot replace already-published differing content. Preserve the original archives
outside Actions if publication may be delayed beyond retention.

A partial mirror is not eligible for installation, but it does not block publication
when another configured registry has the complete verified release. If no complete
registry remains, publication fails. Retrying an already
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
checkpoint and update one bot PR. Review and merge it to publish to the existing documentation server at
`docs.flowsteward.org`. The site builds with that origin and `BASE_URL=/`; no
GitHub Pages or DNS migration is required. Configure its scoped SSH deployment
identity and verified host key as described in the documentation repository.
See the documentation repository README for review, failure and retry behavior.
