#!/bin/sh
set -eu
port="${FS_RUNTIME_HTTP_PORT:?FS_RUNTIME_HTTP_PORT is required}"
case "$port" in
  ''|*[!0-9]*) echo "FS_RUNTIME_HTTP_PORT must be a decimal port" >&2; exit 78 ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
  echo "FS_RUNTIME_HTTP_PORT must be between 1 and 65535" >&2
  exit 78
fi
sed "s/__FS_RUNTIME_HTTP_PORT__/$port/g" \
  /etc/traefik/templates/public-router.yaml.tmpl \
  > /run/traefik-dynamic/public-router.yaml
exec /entrypoint.sh "$@"
