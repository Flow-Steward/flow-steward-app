#!/bin/sh
set -eu
files=/etc/traefik/files
port="${FS_RUNTIME_HTTP_PORT:?FS_RUNTIME_HTTP_PORT is required}"
case "$port" in
  ''|*[!0-9]*) echo "FS_RUNTIME_HTTP_PORT must be a decimal port" >&2; exit 78 ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
  echo "FS_RUNTIME_HTTP_PORT must be between 1 and 65535" >&2
  exit 78
fi
sed "s/__FS_RUNTIME_HTTP_PORT__/$port/g" \
  "$files/public-router.yaml.tmpl" \
  > /run/traefik-dynamic/public-router.yaml
# File-based TLS (tls-bundled-files.yaml) ships its dynamic configuration beside
# the certificates; ACME and reverse-proxy modes have none.
if [ -d "$files/dynamic" ]; then
  cp "$files"/dynamic/*.yaml /run/traefik-dynamic/
fi
exec /entrypoint.sh "$@"
