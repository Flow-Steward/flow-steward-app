#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
exec python3 "$root/scripts/release/official_release.py" build --root "$root" \
  --version "${FS_RELEASE_VERSION:?set FS_RELEASE_VERSION}" \
  --revision "${FS_RELEASE_REVISION:?set FS_RELEASE_REVISION}" \
  --platform "${FS_RELEASE_PLATFORM:?set FS_RELEASE_PLATFORM}" \
  --artifact-dir "${FS_RELEASE_ARTIFACT_DIR:?set FS_RELEASE_ARTIFACT_DIR}"
