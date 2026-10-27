#!/usr/bin/env bash
# Build or reuse one finalized candidate, start it, then run safe smoke exactly once.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BUILD_SCRIPT="${FS_RELEASE_BUILD_SCRIPT:-$SCRIPT_DIR/build_candidate.sh}"
PREPARE_SCRIPT="${FS_RELEASE_PREPARE_SCRIPT:-$SCRIPT_DIR/candidate_prepare.sh}"
STACK_SCRIPT="${FS_RELEASE_STACK_SCRIPT:-$SCRIPT_DIR/candidate_stack.sh}"
VERIFY_SCRIPT="${FS_RELEASE_VERIFY_SCRIPT:-$SCRIPT_DIR/candidate_verify.sh}"
FS_RELEASE_CANDIDATE_COMMIT="${FS_RELEASE_CANDIDATE_COMMIT:-$(git -C "$SCRIPT_DIR/../.." rev-parse HEAD^{commit})}"
FS_RELEASE_RUN_ID="${FS_RELEASE_RUN_ID:-publish-$(printf '%s' "$FS_RELEASE_CANDIDATE_COMMIT" | cut -c1-12)-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
export FS_RELEASE_CANDIDATE_COMMIT FS_RELEASE_RUN_ID

for script in "$BUILD_SCRIPT" "$PREPARE_SCRIPT" "$STACK_SCRIPT" "$VERIFY_SCRIPT"; do
  [ -x "$script" ] || { printf 'candidate publication script is not executable: %s\n' "$script" >&2; exit 2; }
done

"$BUILD_SCRIPT"
"$PREPARE_SCRIPT" all
"$STACK_SCRIPT" --profile cli up -d --no-build
"$VERIFY_SCRIPT" --safe
printf 'candidate publication safe verification passed commit=%s\n' \
  "$FS_RELEASE_CANDIDATE_COMMIT"
printf 'candidate publication evidence run=%s\n' "$FS_RELEASE_RUN_ID"
