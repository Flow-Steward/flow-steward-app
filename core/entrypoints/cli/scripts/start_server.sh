#!/bin/bash
# Start the Flow Steward FastAPI server with HTTP by default.
#
# When FS_SERVE_SSL=1 is set, generates a self-signed SSL certificate valid for 10 years
# (no passphrase, includes SAN for localhost and 127.0.0.1).
# On subsequent runs: reuses the existing certificate.
#
# Usage:
#   docker compose -f infra/docker/docker-compose.yaml exec fs-app /app/core/entrypoints/cli/scripts/start_server.sh
#   docker compose -f infra/docker/docker-compose.yaml exec fs-app /app/core/entrypoints/cli/scripts/start_server.sh --port 8099
#   FS_SERVE_SSL=1 docker compose -f infra/docker/docker-compose.yaml exec fs-app /app/core/entrypoints/cli/scripts/start_server.sh
#
# The certificate is stored at /app/.ssl/ (gitignored) and mounted via the
# .:/app volume, so it persists across container restarts.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
PORT="${PORT:-8099}"
WORKERS="${WORKERS:-1}"
SERVE_SSL="${FS_SERVE_SSL:-0}"
AUTO_RELOAD="${FS_AUTO_RELOAD:-0}"
FRONTEND_DIR="${FRONTEND_DIR:-$APP_ROOT/assets/frontend}"
FRONTEND_DIST_MANIFEST="$FRONTEND_DIR/dist/.vite/manifest.json"

# shellcheck source=core/entrypoints/cli/scripts/frontend_shell_freshness.sh
source "$SCRIPT_DIR/frontend_shell_freshness.sh"

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

frontend_shell_dependencies_stale() {
  if [[ ! -f "$FRONTEND_DIST_MANIFEST" ]]; then
    return 0
  fi

  local path
  for path in \
    "$FRONTEND_DIR/package.json" \
    "$FRONTEND_DIR/package-lock.json"
  do
    if [[ -f "$path" && "$path" -nt "$FRONTEND_DIST_MANIFEST" ]]; then
      return 0
    fi
  done

  return 1
}

ensure_frontend_shell_build() {
  if ! frontend_shell_build_required "$FRONTEND_DIR"; then
    return
  fi

  echo "[start_server] Frontend shell dist is missing or stale; rebuilding assets/frontend/dist"
  if ! command -v npm >/dev/null 2>&1; then
    # The app image is Python-only and ships a prebuilt dist, which the /app
    # bind mount hides. Failing here with "npm: command not found" under
    # `restart: always` produced a crash loop that named nothing; the asset is
    # genuinely missing, so this still fails, but says what to do about it.
    echo "[start_server] No npm in this image, so the shell cannot be built here." >&2
    echo "[start_server] assets/frontend/dist is missing because the /app bind mount" >&2
    echo "[start_server] hides the copy baked into the image. Build it once with:" >&2
    echo "[start_server]   docker compose -f infra/docker/docker-compose.yaml run --rm frontend-assets" >&2
    echo "[start_server] A normal 'compose up' does this for you." >&2
    return 1
  fi
  cd "$FRONTEND_DIR"
  if [[ ! -x node_modules/.bin/vite ]] || frontend_shell_dependencies_stale; then
    echo "[start_server] Installing frontend dependencies with npm ci"
    npm ci
  fi
  if ! npm run build; then
    if is_truthy "$AUTO_RELOAD"; then
      echo "[start_server] Frontend build failed in auto-reload mode; continuing with the existing shell assets"
      cd "$APP_ROOT"
      return
    fi
    cd "$APP_ROOT"
    return 1
  fi
  cd "$APP_ROOT"
}

# Parse --port flag if provided
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --no-ssl) SERVE_SSL="0"; shift ;;
    *) shift ;;
  esac
done

ensure_frontend_shell_build

cd "$APP_ROOT"

# External review P2: also the proxies the operator configured (FS_PROXY_TRUSTED_IPS, which
# `config` records and Compact hands to its router). Without them an external Nginx,
# Apache or shared Traefik reached fs-app untrusted: clients looked like the proxy and
# HTTPS looked like HTTP. The configuration's own rule holds here too -- a wildcard
# network or an unreadable entry refuses to start rather than trusting everyone.
FORWARDED_ALLOW_IPS="$(${PYTHON:-python} - <<'PY'
import ipaddress
import os
import socket
import sys

hosts = {"127.0.0.1", "::1"}
for entry in (os.environ.get("FS_PROXY_TRUSTED_IPS") or "").split(","):
    entry = entry.strip()
    if not entry:
        continue
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        sys.exit(f"[start_server] FS_PROXY_TRUSTED_IPS entry {entry!r} is not an address or network")
    if network.prefixlen == 0:
        sys.exit(f"[start_server] FS_PROXY_TRUSTED_IPS must not contain a wildcard network: {entry}")
    hosts.add(str(network))
for hostname in ("traefik",):
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        continue
    for address in addresses:
        try:
            hosts.add(str(ipaddress.ip_address(address[4][0])))
        except ValueError:
            continue
print(",".join(sorted(hosts)))
PY
)"

UVICORN_ARGS=(
  core.entrypoints.api.app:app
  --host 0.0.0.0
  --port "$PORT"
  --proxy-headers
  --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
)

if is_truthy "$AUTO_RELOAD"; then
  UVICORN_ARGS+=(
    --reload
    --reload-dir "$APP_ROOT"
    --reload-exclude "$APP_ROOT/logs/*"
    --reload-exclude "$APP_ROOT/logs/**/*"
    --reload-exclude "$APP_ROOT/.pytest_cache/*"
    --reload-exclude "$APP_ROOT/assets/frontend/dist/*"
  )
else
  UVICORN_ARGS+=(--workers "$WORKERS")
fi

if [[ "$SERVE_SSL" == "0" || "$SERVE_SSL" == "false" || "$SERVE_SSL" == "no" || "$SERVE_SSL" == "off" ]]; then
  if is_truthy "$AUTO_RELOAD"; then
    echo "[start_server] Starting uvicorn on http://0.0.0.0:${PORT} with auto-reload"
  else
    echo "[start_server] Starting uvicorn on http://0.0.0.0:${PORT} (workers=${WORKERS})"
  fi
  echo "[start_server] Dashboard: http://localhost:${PORT}/projects"
  echo ""
  exec uvicorn "${UVICORN_ARGS[@]}"
fi

CERT_DIR="/app/.ssl"
CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"

mkdir -p "$CERT_DIR"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "[start_server] Generating self-signed SSL certificate (10 years)…"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days 3650 \
    -nodes \
    -subj "/CN=flow-steward-local/O=Flow Steward/C=US" \
    -addext "subjectAltName=DNS:localhost,DNS:fs-app,IP:127.0.0.1,IP:0.0.0.0"
  echo "[start_server] Certificate written to $CERT_DIR"
  echo "[start_server] SHA1 fingerprint:"
  openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha1 2>/dev/null || true
else
  echo "[start_server] Reusing existing certificate at $CERT_DIR"
  EXPIRY=$(openssl x509 -in "$CERT_FILE" -noout -enddate 2>/dev/null | cut -d= -f2 || echo "unknown")
  echo "[start_server] Certificate expires: $EXPIRY"
fi

if is_truthy "$AUTO_RELOAD"; then
  echo "[start_server] Starting uvicorn on https://0.0.0.0:${PORT} with auto-reload"
else
  echo "[start_server] Starting uvicorn on https://0.0.0.0:${PORT} (workers=${WORKERS})"
fi
echo "[start_server] Dashboard: https://localhost:${PORT}/projects"
echo ""

exec uvicorn "${UVICORN_ARGS[@]}" \
  --ssl-keyfile "$KEY_FILE" \
  --ssl-certfile "$CERT_FILE"
