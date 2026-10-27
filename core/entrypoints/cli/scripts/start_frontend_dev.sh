#!/bin/bash

set -euo pipefail

FRONTEND_DIR="/app/assets/frontend"
HOST="${VITE_HOST:-0.0.0.0}"
PORT="${VITE_PORT:-5173}"
AUTO_RELOAD="${FS_AUTO_RELOAD:-0}"

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

if ! is_truthy "$AUTO_RELOAD"; then
  echo "[start_frontend_dev] FS_AUTO_RELOAD is disabled; frontend dev server is idle"
  exec tail -f /dev/null
fi

cd "$FRONTEND_DIR"

if [[ ! -x node_modules/.bin/vite ]]; then
  echo "[start_frontend_dev] Installing frontend dependencies with npm ci"
  npm ci
fi

echo "[start_frontend_dev] Starting Vite dev server on http://localhost:${PORT}/app/"

exec npm run dev -- --host "$HOST" --port "$PORT" --strictPort
