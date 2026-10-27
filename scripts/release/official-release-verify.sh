#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
exec python3 "$root/scripts/release/official_release.py" verify --root "$root" \
  --version "${FS_RELEASE_VERSION:?set FS_RELEASE_VERSION}" \
  --revision "${FS_RELEASE_REVISION:?set FS_RELEASE_REVISION}" \
  --evidence "${FS_RELEASE_EVIDENCE:?set FS_RELEASE_EVIDENCE}"
