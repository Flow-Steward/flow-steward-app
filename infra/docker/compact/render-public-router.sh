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
# #27: traefik-files copies what Traefik needs into this volume. A container created
# before that change still bind-mounts this script from an updated checkout, and may
# restart before the update recreates it, so it keeps working from its old mounts.
files=/etc/traefik/files
if [ -d "$files" ]; then
  template="$files/public-router.yaml.tmpl"
  # The certificate configuration names /etc/traefik/certs in both layouts.
  if [ -d "$files/certs" ]; then
    ln -sfn "$files/certs" /etc/traefik/certs
  fi
  # File-based TLS (tls-bundled-files.yaml) ships its dynamic configuration beside
  # the certificates; ACME and reverse-proxy modes have none.
  if [ -d "$files/dynamic" ]; then
    cp "$files"/dynamic/*.yaml /run/traefik-dynamic/
  fi
else
  template=/etc/traefik/templates/public-router.yaml.tmpl
fi
sed "s/__FS_RUNTIME_HTTP_PORT__/$port/g" "$template" > /run/traefik-dynamic/public-router.yaml
exec /entrypoint.sh "$@"
