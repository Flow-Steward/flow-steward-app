#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

BATCH_SIZE="${ESLINT_BATCH_SIZE:-32}"
MODE="${1:-check}"
FIX_FLAG=""

if [[ "${MODE}" == "fix" ]]; then
  FIX_FLAG="--fix"
fi

FILES=()
while IFS= read -r -d '' file; do
  FILES+=("${file}")
done < <(find src -type f \( -name '*.ts' -o -name '*.tsx' \) -print0 | sort -z)
TOTAL_FILES="${#FILES[@]}"

if (( TOTAL_FILES == 0 )); then
  echo "[frontend-lint] No TypeScript source files found under src/"
else
  TOTAL_BATCHES=$(( (TOTAL_FILES + BATCH_SIZE - 1) / BATCH_SIZE ))
  for (( i=0; i<TOTAL_FILES; i+=BATCH_SIZE )); do
    BATCH=( "${FILES[@]:i:BATCH_SIZE}" )
    BATCH_NUMBER=$(( i / BATCH_SIZE + 1 ))
    echo "[frontend-lint] Batch ${BATCH_NUMBER}/${TOTAL_BATCHES} (${#BATCH[@]} files)"
    eslint --max-warnings=0 ${FIX_FLAG} "${BATCH[@]}"
  done
fi

echo "[frontend-lint] Config files"
eslint --max-warnings=0 ${FIX_FLAG} playwright.config.ts vite.config.ts vitest.config.ts
