#!/usr/bin/env bash
# Build one immutable candidate and write its non-secret durable environment and
# provenance files under .flow-steward/release/<commit>/.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

git diff --quiet || { printf 'tracked working tree changes must be committed before candidate build\n' >&2; exit 2; }
git diff --cached --quiet || { printf 'staged changes must be committed before candidate build\n' >&2; exit 2; }

COMMIT="$(git rev-parse HEAD^{commit})"
SHORT="$(printf '%s' "$COMMIT" | cut -c1-12)"
APP_IMAGE="flow-steward-app:candidate-$SHORT"
WORKER_IMAGE="flow-steward-worker:candidate-$SHORT"
CONTROL_IMAGE="flow-steward-control:candidate-$SHORT"
CLI_IMAGE="flow-steward-cli:candidate-$SHORT"
MCP_IMAGE="flow-steward-mcp:candidate-$SHORT"
POSTGRES_BUILD_IMAGE="flow-steward-postgres:candidate-$SHORT"
COMPOSE_PROJECT="fs-candidate-$SHORT"
PYTHON_BASE_IMAGE="python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
POSTGRES_BASE_IMAGE="pgvector/pgvector:0.8.5-pg17@sha256:d2ef61f42ef767baa5a1475393303cc235bcd92febd9d7014eddb48b41f3bad0"
TRAEFIK_IMAGE="traefik:v3.3@sha256:2cd5cc75530c8d07ae0587c743d23eb30cae2436d07017a5ff78498b1a43d09f"
RABBITMQ_IMAGE="rabbitmq:3.13-management@sha256:e582c0bc7766f3342496d8485efb5a1df782b5ce3886ad017e2eaae442311f69"
MAILPIT_IMAGE="axllent/mailpit@sha256:757f22b56c1da03570afdb3d259effe5091018008a81bbedc8158cee7e16fdbc"
SEAWEEDFS_IMAGE="chrislusf/seaweedfs@sha256:854479eebcbc0060d803edb27b3bd88a0552e23fde08a26a6482e59aff887a77"
RELEASE_DIR="$REPO_ROOT/.flow-steward/release/$COMMIT"
ENV_FILE="$RELEASE_DIR/candidate.env"
PROVENANCE_FILE="$RELEASE_DIR/candidate-provenance.json"
SOURCE_MANIFEST="$RELEASE_DIR/candidate-source.json"
IMAGE_ARCHIVE="$RELEASE_DIR/candidate-images.tar"
IMAGE_ARCHIVE_MANIFEST="$RELEASE_DIR/candidate-images.json"
PRODUCTION_ENV="$RELEASE_DIR/production.env"
CANDIDATE_STATE_DIR="$RELEASE_DIR/candidate-state"
CANDIDATE_RUNTIME_DIR="$RELEASE_DIR/runtime"
CANDIDATE_EXTENSIONS_DIR="$RELEASE_DIR/candidate-extensions"
CANDIDATE_RESOURCE_ARGS=()
for service in \
  traefik rabbitmq-fs mailpit pg-fs memory-postgres-extensions \
  fs-mcp fs-app fs-cli fs-worker control-plane seaweedfs; do
  CANDIDATE_RESOURCE_ARGS+=(--resource "containers=$COMPOSE_PROJECT-$service-1")
done
for network in default artifact-storage; do
  CANDIDATE_RESOURCE_ARGS+=(--resource "networks=${COMPOSE_PROJECT}_$network")
done
for volume in fs-postgres fs-rabbitmq seaweedfs-data; do
  CANDIDATE_RESOURCE_ARGS+=(--resource "volumes=${COMPOSE_PROJECT}_$volume")
done
PROD_ENV="${FS_RELEASE_PROD_ENV:-$REPO_ROOT/.env.prod}"
case "$PROD_ENV" in
  /*) ;;
  *) PROD_ENV="$(CDPATH= cd -- "$(dirname -- "$PROD_ENV")" && pwd)/$(basename -- "$PROD_ENV")" ;;
esac
TLS_CERT_INPUT="${FS_RELEASE_TLS_CERT:-$REPO_ROOT/.ssl/cert.pem}"
TLS_KEY_INPUT="${FS_RELEASE_TLS_KEY:-$REPO_ROOT/.ssl/key.pem}"
# AC17-R143: the certificate and key are deployment inputs only where this deployment
# holds them as files. Behind an existing Traefik, or with ACME, TLS belongs to the
# ingress and no such file exists, so demanding one made those installs impossible.
# The caller says which it is -- the installer knows the TLS mode, this script does
# not -- and the default stays "local", so a missing certificate where one is required
# still fails exactly as before. Inferring it from whether the files happen to exist
# would turn that failure into silence.
case "${FS_RELEASE_TLS_INPUTS:-local}" in
  local)
    TLS_INPUT_ARGS=(
      --deployment-input-source "TLS_CERT=$TLS_CERT_INPUT"
      --deployment-input-source "TLS_KEY=$TLS_KEY_INPUT"
    )
    ;;
  ingress)
    TLS_INPUT_ARGS=()
    ;;
  *)
    printf 'FS_RELEASE_TLS_INPUTS must be "local" or "ingress", not "%s"\n' \
      "$FS_RELEASE_TLS_INPUTS" >&2
    exit 2
    ;;
esac
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$(CDPATH= cd -- "$GIT_COMMON_DIR" && pwd)" ;;
esac

snapshot_args=(
  python3 -m core.entrypoints.cli.ops.candidate_harness snapshot
  --root "$REPO_ROOT"
  --candidate "$COMMIT"
  --source-env "$PROD_ENV"
  --materialize
)
if [ -n "${FS_RELEASE_APP_PORT:-}" ] || [ -n "${FS_RELEASE_HTTP_PORT:-}" ] || \
  [ -n "${FS_RELEASE_MAILPIT_HTTP_PORT:-}" ]; then
  snapshot_args+=(
    --app-port "${FS_RELEASE_APP_PORT:-}"
    --http-port "${FS_RELEASE_HTTP_PORT:-}"
    --mailpit-port "${FS_RELEASE_MAILPIT_HTTP_PORT:-}"
  )
fi
# AC17-R159: under `set -e` a failing assignment exited with the harness's answer still
# in the variable -- the operator saw "exit code 2" and nothing else. Say why.
if ! SNAPSHOT_OUTPUT="$("${snapshot_args[@]}")"; then
  printf 'candidate snapshot failed: %s\n' "${SNAPSHOT_OUTPUT:-the harness printed nothing}" >&2
  exit 2
fi
FS_RELEASE_APP_PORT="$(printf '%s' "$SNAPSHOT_OUTPUT" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["plain_inputs"]["APP_PORT"])')"
FS_RELEASE_HTTP_PORT="$(printf '%s' "$SNAPSHOT_OUTPUT" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["plain_inputs"]["HTTP_PORT"])')"
FS_RELEASE_MAILPIT_HTTP_PORT="$(printf '%s' "$SNAPSHOT_OUTPUT" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["plain_inputs"]["MAILPIT_HTTP_PORT"])')"

candidate_env_value() {
  key="$1"
  awk -v expected="$key" '
    index($0, expected "=") == 1 { value = substr($0, length(expected) + 2) }
    END { print value }
  ' "$ENV_FILE"
}

FINALIZED_FILE="$RELEASE_DIR/candidate-finalized.json"
mkdir -p "$RELEASE_DIR"
umask 077
# AC17-R116, ninth obstacle: this tree is handed to a nested container, and a
# container cannot share its own ephemeral filesystem with one it starts. Docker
# resolves `--volume` against the host, where a path created in this container's
# /tmp does not exist, so the nested run received a silently created empty
# directory and could not find the file it was built to read. Under RELEASE_DIR,
# like the three other temporary paths here, it is inside the one directory the
# host and both containers all agree on.
SOURCE_ROOT="$(mktemp -d "$RELEASE_DIR/.candidate-source.XXXXXX")"
TMP_SOURCE_MANIFEST="$(mktemp "$RELEASE_DIR/.candidate-source.json.XXXXXX")"
cleanup_candidate_source() {
  rm -rf -- "$SOURCE_ROOT"
  rm -f -- "$TMP_SOURCE_MANIFEST"
}
trap cleanup_candidate_source EXIT
python3 scripts/release/prepare_candidate_source.py \
  --root "$REPO_ROOT" \
  --commit "$COMMIT" \
  --output "$SOURCE_ROOT" \
  --manifest "$TMP_SOURCE_MANIFEST" >/dev/null
python3 "$SOURCE_ROOT/scripts/release/sync_extension_sdk_wheel.py" \
  --root "$SOURCE_ROOT" --check >/dev/null

for pinned_image in \
  "$PYTHON_BASE_IMAGE" "$POSTGRES_BASE_IMAGE" "$TRAEFIK_IMAGE" "$RABBITMQ_IMAGE" \
  "$MAILPIT_IMAGE" "$SEAWEEDFS_IMAGE"; do
  docker image inspect "$pinned_image" >/dev/null 2>&1 || docker pull "$pinned_image" >/dev/null
done

case "$(uname -m)" in
  aarch64|arm64) CANDIDATE_ARCHITECTURE="linux/arm64" ;;
  x86_64|amd64) CANDIDATE_ARCHITECTURE="linux/amd64" ;;
  *)
    printf 'unsupported Docker architecture for release candidate\n' >&2
    exit 2
    ;;
esac

if [ -s "$ENV_FILE" ] && [ -s "$PROVENANCE_FILE" ] && [ -s "$FINALIZED_FILE" ] && \
  [ -s "$SOURCE_MANIFEST" ] && [ -s "$IMAGE_ARCHIVE" ] && \
  [ -s "$IMAGE_ARCHIVE_MANIFEST" ]; then
  cmp -s "$TMP_SOURCE_MANIFEST" "$SOURCE_MANIFEST" || {
    printf 'recorded candidate source identity does not match commit %s\n' "$COMMIT" >&2
    exit 2
  }
  APP_DIGEST="$(candidate_env_value FS_APP_IMAGE)"
  WORKER_DIGEST="$(candidate_env_value FS_WORKER_IMAGE)"
  CONTROL_DIGEST="$(candidate_env_value FS_CONTROL_IMAGE)"
  CLI_DIGEST="$(candidate_env_value FS_CLI_IMAGE)"
  MCP_DIGEST="$(candidate_env_value MCP_IMAGE)"
  POSTGRES_DIGEST="$(candidate_env_value POSTGRES_IMAGE)"
  CANDIDATE_EXTENSIONS_DIR="$(candidate_env_value FS_EXTENSIONS_DIR)"
  python3 "$SOURCE_ROOT/scripts/release/candidate_image_archive.py" verify \
    --archive "$IMAGE_ARCHIVE" \
    --manifest "$IMAGE_ARCHIVE_MANIFEST" \
    --candidate-commit "$COMMIT" \
    --image "fs-app=$APP_DIGEST" \
    --image "fs-worker=$WORKER_DIGEST" \
    --image "control-plane=$CONTROL_DIGEST" \
    --image "fs-cli=$CLI_DIGEST" \
    --image "fs-mcp=$MCP_DIGEST" \
    --image "postgres=$POSTGRES_DIGEST" >/dev/null
  for digest in "$APP_DIGEST" "$WORKER_DIGEST" "$CONTROL_DIGEST" \
    "$CLI_DIGEST" "$MCP_DIGEST" "$POSTGRES_DIGEST"; do
    [ -n "$digest" ] && docker image inspect "$digest" >/dev/null || {
      printf 'finalized candidate image is unavailable: %s\n' "${digest:-<missing>}" >&2
      exit 2
    }
  done
  python3 "$SOURCE_ROOT/scripts/release/verify_candidate_provenance.py" \
    --root "$SOURCE_ROOT" \
    --provenance "$PROVENANCE_FILE" \
    --source-manifest "$SOURCE_MANIFEST" \
    --candidate-commit "$COMMIT" \
    --verify-images
  python3 -m core.entrypoints.cli.ops.candidate_harness snapshot \
    --root "$REPO_ROOT" \
    --candidate "$COMMIT" \
    --finalize \
    --image-digest "fs-app=$APP_DIGEST" \
    --image-digest "fs-worker=$WORKER_DIGEST" \
    --image-digest "control-plane=$CONTROL_DIGEST" \
    --image-digest "fs-cli=$CLI_DIGEST" \
    --image-digest "fs-mcp=$MCP_DIGEST" \
    --image-tag "$APP_IMAGE" \
    --image-tag "$WORKER_IMAGE" \
    --image-tag "$CONTROL_IMAGE" \
    --image-tag "$CLI_IMAGE" \
    --image-tag "$MCP_IMAGE" \
    --image-tag "$POSTGRES_BUILD_IMAGE" \
    "${CANDIDATE_RESOURCE_ARGS[@]}" \
    --compose-working-dir "$REPO_ROOT/infra/docker" \
    --compose-config-files "$REPO_ROOT/infra/docker/docker-compose.prod.yaml" \
    --extension-dir "$CANDIDATE_EXTENSIONS_DIR" \
    --provenance-file "$PROVENANCE_FILE" >/dev/null
  printf 'candidate_reused=true\n'
  printf 'candidate_commit=%s\n' "$COMMIT"
  printf 'candidate_env=%s\n' "$ENV_FILE"
  printf 'candidate_provenance=%s\n' "$PROVENANCE_FILE"
  cleanup_candidate_source
  trap - EXIT
  exit 0
fi

if [ -e "$ENV_FILE" ] || [ -e "$PROVENANCE_FILE" ] || [ -e "$SOURCE_MANIFEST" ]; then
  printf 'candidate identity is already recorded for commit %s; refusing to overwrite it\n' \
    "$COMMIT" >&2
  exit 2
fi

TMP_ENV_FILE="$(mktemp "$RELEASE_DIR/.candidate.env.XXXXXX")"
TMP_PROVENANCE_FILE="$(mktemp "$RELEASE_DIR/.candidate-provenance.json.XXXXXX")"
TMP_IMAGE_ARCHIVE="$(mktemp "$RELEASE_DIR/.candidate-images.tar.XXXXXX")"
TMP_IMAGE_ARCHIVE_MANIFEST="$(mktemp "$RELEASE_DIR/.candidate-images.json.XXXXXX")"
TMP_EXTENSIONS_DIR="$(mktemp -d "$RELEASE_DIR/.candidate-extensions.XXXXXX")"
cleanup_partial_candidate() {
  rm -f -- "$TMP_ENV_FILE" "$TMP_PROVENANCE_FILE" \
    "$TMP_IMAGE_ARCHIVE" "$TMP_IMAGE_ARCHIVE_MANIFEST"
  rm -rf -- "$SOURCE_ROOT"
  rm -f -- "$TMP_SOURCE_MANIFEST" "$SOURCE_MANIFEST"
  rm -rf -- "$TMP_EXTENSIONS_DIR" "$CANDIDATE_EXTENSIONS_DIR"
  rm -f -- "$PRODUCTION_ENV"
  rm -rf -- "$CANDIDATE_STATE_DIR" "$CANDIDATE_RUNTIME_DIR"
  rm -f -- "$RELEASE_DIR/snapshot-key" "$RELEASE_DIR/candidate-snapshot.json" \
    "$RELEASE_DIR/candidate-finalized.json" "$IMAGE_ARCHIVE" \
    "$IMAGE_ARCHIVE_MANIFEST"
  python3 - "$REPO_ROOT" "$COMMIT" <<'PY'
import sys
from pathlib import Path

from core.infrastructure.release_candidate.host_gateway import EndpointAllocator

root, candidate = sys.argv[1:]
EndpointAllocator(Path(root) / ".flow-steward" / "release" / "endpoint-state").release(candidate)
PY
  rmdir "$RELEASE_DIR" >/dev/null 2>&1 || true
}
trap cleanup_partial_candidate EXIT

docker build --target app-runtime --build-arg "BUILD_SHA=$COMMIT" \
  --file "$SOURCE_ROOT/infra/docker/Dockerfile" --tag "$APP_IMAGE" "$SOURCE_ROOT"
docker build --target worker-runtime --build-arg "BUILD_SHA=$COMMIT" \
  --file "$SOURCE_ROOT/infra/docker/Dockerfile" --tag "$WORKER_IMAGE" "$SOURCE_ROOT"
docker build --target control-runtime --build-arg "BUILD_SHA=$COMMIT" \
  --file "$SOURCE_ROOT/infra/docker/Dockerfile" --tag "$CONTROL_IMAGE" "$SOURCE_ROOT"
docker build --target cli --build-arg "FS_BUILD_REVISION=$COMMIT" \
  --file "$SOURCE_ROOT/infra/docker/Dockerfile" --tag "$CLI_IMAGE" "$SOURCE_ROOT"
docker build --target runtime --build-arg "FS_BUILD_REVISION=$COMMIT" \
  --file "$SOURCE_ROOT/infra/mcp/Dockerfile" --tag "$MCP_IMAGE" "$SOURCE_ROOT"
docker build --build-arg "AGE_REF=PG17/v1.6.0-rc0" \
  --label "org.opencontainers.image.revision=$COMMIT" \
  --file "$SOURCE_ROOT/infra/docker/postgres-extended/Dockerfile" \
  --tag "$POSTGRES_BUILD_IMAGE" "$SOURCE_ROOT"

APP_DIGEST="$(docker image inspect "$APP_IMAGE" --format '{{.Id}}')"
WORKER_DIGEST="$(docker image inspect "$WORKER_IMAGE" --format '{{.Id}}')"
CONTROL_DIGEST="$(docker image inspect "$CONTROL_IMAGE" --format '{{.Id}}')"
CLI_DIGEST="$(docker image inspect "$CLI_IMAGE" --format '{{.Id}}')"
MCP_DIGEST="$(docker image inspect "$MCP_IMAGE" --format '{{.Id}}')"
POSTGRES_DIGEST="$(docker image inspect "$POSTGRES_BUILD_IMAGE" --format '{{.Id}}')"

# A release ships no extensions: each one lives in its own repository and installs
# from the extension catalog. The candidate starts, like a fresh install, with an
# empty extensions directory.
mv -- "$TMP_EXTENSIONS_DIR" "$CANDIDATE_EXTENSIONS_DIR"

{
  printf 'FS_BUILD_SHA=%s\n' "$COMMIT"
  printf 'FS_BUILD_REVISION=%s\n' "$COMMIT"
  printf 'FS_CLI_RUNTIME_REVISION=%s\n' "$COMMIT"
  printf 'FS_APP_IMAGE=%s\n' "$APP_DIGEST"
  printf 'FS_WORKER_IMAGE=%s\n' "$WORKER_DIGEST"
  printf 'FS_CONTROL_IMAGE=%s\n' "$CONTROL_DIGEST"
  printf 'FS_CLI_IMAGE=%s\n' "$CLI_DIGEST"
  printf 'MCP_IMAGE=%s\n' "$MCP_DIGEST"
  printf 'FS_APP_IMAGE_DIGEST=%s\n' "$APP_DIGEST"
  printf 'FS_WORKER_IMAGE_DIGEST=%s\n' "$WORKER_DIGEST"
  printf 'FS_CONTROL_IMAGE_DIGEST=%s\n' "$CONTROL_DIGEST"
  printf 'MCP_IMAGE_DIGEST=%s\n' "$MCP_DIGEST"
  printf 'POSTGRES_IMAGE=%s\n' "$POSTGRES_DIGEST"
  printf 'TRAEFIK_IMAGE=%s\n' "$TRAEFIK_IMAGE"
  printf 'RABBITMQ_IMAGE=%s\n' "$RABBITMQ_IMAGE"
  printf 'MAILPIT_IMAGE=%s\n' "$MAILPIT_IMAGE"
  printf 'FS_EXTENSIONS_DIR=%s\n' "$CANDIDATE_EXTENSIONS_DIR"
  printf 'APP_PORT=%s\n' "${FS_RELEASE_APP_PORT:-18099}"
  printf 'HTTP_PORT=%s\n' "${FS_RELEASE_HTTP_PORT:-18080}"
  printf 'MAILPIT_HTTP_PORT=%s\n' "${FS_RELEASE_MAILPIT_HTTP_PORT:-18025}"
} >"$TMP_ENV_FILE"
chmod 600 "$TMP_ENV_FILE"

python3 -m scripts.release.prepare_candidate_runtime \
  --source-env "$PROD_ENV" \
  --output-env "$PRODUCTION_ENV" \
  --state-dir "$CANDIDATE_STATE_DIR" \
  --candidate-commit "$COMMIT" \
  --public-port "${FS_RELEASE_APP_PORT:-18099}" \
  --self-hosted-evidence \
  --runtime-overrides "$TMP_ENV_FILE"
chmod 600 "$PRODUCTION_ENV"

# Run inside the caller's canonical installer/quality container: it owns
# Git and PyYAML. Product images deliberately carry no checkout tooling.
python3 scripts/release/generate_candidate_provenance.py \
  --candidate-commit "$COMMIT" \
  --image-digest "fs-app=$APP_DIGEST" \
  --image-digest "fs-worker=$WORKER_DIGEST" \
  --image-digest "control-plane=$CONTROL_DIGEST" \
  --image-digest "fs-cli=$CLI_DIGEST" \
  --image-digest "fs-mcp=$MCP_DIGEST" \
  --deployment-input-source "SECURITY_PII_SALT=$PROD_ENV" \
  --deployment-input-source "FS_METRICS_TOKEN=$PROD_ENV" \
  --deployment-input-source "FS_WORKER_MCP_BEARER_TOKEN=$PROD_ENV" \
  --deployment-input-source "POSTGRES_PASSWORD=$PROD_ENV" \
  --deployment-input-source "RABBITMQ_PASS=$PROD_ENV" \
  ${TLS_INPUT_ARGS[@]+"${TLS_INPUT_ARGS[@]}"} \
  --source-manifest "$TMP_SOURCE_MANIFEST" \
  --sdk-attestation "$SOURCE_ROOT/core/infrastructure/extension_sdk/public/sdk-wheel-attestation.json" \
  --release-input "infra/docker/Dockerfile=$SOURCE_ROOT/infra/docker/Dockerfile" \
  --release-input "infra/mcp/Dockerfile=$SOURCE_ROOT/infra/mcp/Dockerfile" \
  --release-input "infra/docker/postgres-extended/Dockerfile=$SOURCE_ROOT/infra/docker/postgres-extended/Dockerfile" \
  --release-input "infra/docker/docker-compose.prod.yaml=$SOURCE_ROOT/infra/docker/docker-compose.prod.yaml" \
  --release-input "requirements-runtime.lock=$SOURCE_ROOT/requirements-runtime.lock" \
  --release-input "requirements-cli.lock=$SOURCE_ROOT/requirements-cli.lock" \
  --release-input "assets/frontend/package-lock.json=$SOURCE_ROOT/assets/frontend/package-lock.json" \
  --release-input "core/infrastructure/extension_sdk/public/sdk-wheel-attestation.json=$SOURCE_ROOT/core/infrastructure/extension_sdk/public/sdk-wheel-attestation.json" \
  --base-image "python=$PYTHON_BASE_IMAGE" \
  --base-image "postgres=$POSTGRES_BASE_IMAGE" \
  --infrastructure-image "traefik=$TRAEFIK_IMAGE" \
  --infrastructure-image "rabbitmq=$RABBITMQ_IMAGE" \
  --infrastructure-image "mailpit=$MAILPIT_IMAGE" \
  --infrastructure-image "postgres=$POSTGRES_DIGEST" \
  --infrastructure-image "seaweedfs=$SEAWEEDFS_IMAGE" \
  --build-argument "BUILD_SHA=$COMMIT" \
  --build-argument "FS_BUILD_REVISION=$COMMIT" \
  --build-argument "AGE_REF=PG17/v1.6.0-rc0" \
  --architecture "$CANDIDATE_ARCHITECTURE" \
  --output "$TMP_PROVENANCE_FILE" >/dev/null

chmod 600 "$TMP_PROVENANCE_FILE"
docker image save --output "$TMP_IMAGE_ARCHIVE" \
  "$APP_DIGEST" "$WORKER_DIGEST" "$CONTROL_DIGEST" \
  "$CLI_DIGEST" "$MCP_DIGEST" "$POSTGRES_DIGEST"
chmod 600 "$TMP_IMAGE_ARCHIVE"
python3 "$SOURCE_ROOT/scripts/release/candidate_image_archive.py" create \
  --archive "$TMP_IMAGE_ARCHIVE" \
  --manifest "$TMP_IMAGE_ARCHIVE_MANIFEST" \
  --candidate-commit "$COMMIT" \
  --image "fs-app=$APP_DIGEST" \
  --image "fs-worker=$WORKER_DIGEST" \
  --image "control-plane=$CONTROL_DIGEST" \
  --image "fs-cli=$CLI_DIGEST" \
  --image "fs-mcp=$MCP_DIGEST" \
  --image "postgres=$POSTGRES_DIGEST"
mv -- "$TMP_PROVENANCE_FILE" "$PROVENANCE_FILE"
mv -- "$TMP_SOURCE_MANIFEST" "$SOURCE_MANIFEST"
mv -- "$TMP_ENV_FILE" "$ENV_FILE"
mv -- "$TMP_IMAGE_ARCHIVE" "$IMAGE_ARCHIVE"
mv -- "$TMP_IMAGE_ARCHIVE_MANIFEST" "$IMAGE_ARCHIVE_MANIFEST"
python3 -m core.entrypoints.cli.ops.candidate_harness snapshot \
  --root "$REPO_ROOT" \
  --candidate "$COMMIT" \
  --finalize \
  --image-digest "fs-app=$APP_DIGEST" \
  --image-digest "fs-worker=$WORKER_DIGEST" \
  --image-digest "control-plane=$CONTROL_DIGEST" \
  --image-digest "fs-cli=$CLI_DIGEST" \
  --image-digest "fs-mcp=$MCP_DIGEST" \
  --image-tag "$APP_IMAGE" \
  --image-tag "$WORKER_IMAGE" \
  --image-tag "$CONTROL_IMAGE" \
  --image-tag "$CLI_IMAGE" \
  --image-tag "$MCP_IMAGE" \
  --image-tag "$POSTGRES_BUILD_IMAGE" \
  "${CANDIDATE_RESOURCE_ARGS[@]}" \
  --compose-working-dir "$REPO_ROOT/infra/docker" \
  --compose-config-files "$REPO_ROOT/infra/docker/docker-compose.prod.yaml" \
  --extension-dir "$CANDIDATE_EXTENSIONS_DIR" \
  --provenance-file "$PROVENANCE_FILE" >/dev/null
trap - EXIT
rm -rf -- "$SOURCE_ROOT"

printf 'candidate_commit=%s\n' "$COMMIT"
printf 'candidate_env=%s\n' "$ENV_FILE"
printf 'candidate_provenance=%s\n' "$PROVENANCE_FILE"
