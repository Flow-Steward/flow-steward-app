#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || ! "$1" =~ ^[1-9][0-9]*$ ]]; then
  echo 'Usage: merge-release-pr.sh <pull-request-number>' >&2
  exit 2
fi

repository=Flow-Steward/flow-steward-app
pr_number="$1"
candidate="$(gh pr view "$pr_number" --repo "$repository" \
  --json baseRefName,headRefName,headRefOid,isCrossRepository,isDraft,state \
  --jq 'select(.state == "OPEN" and .baseRefName == "main" and .isCrossRepository == false and .isDraft == false) | [.headRefName, .headRefOid] | @tsv')"
IFS=$'\t' read -r branch head_sha <<< "$candidate"
if [[ -z "$branch" || ! "$head_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Expected an open, ready release PR from this repository into main.' >&2
  exit 1
fi

require_passed_run() {
  local workflow="$1" expected_title="$2"
  local run run_sha run_status conclusion title
  run="$(gh run list --repo "$repository" --workflow "$workflow" \
    --branch "$branch" --commit "$head_sha" --event push --limit 1 \
    --json headSha,status,conclusion,displayTitle \
    --jq '.[0] | select(. != null) | [.headSha, .status, .conclusion, .displayTitle] | @tsv')"
  IFS=$'\t' read -r run_sha run_status conclusion title <<< "$run"
  if [[ "$run_sha" != "$head_sha" || "$run_status" != completed || "$conclusion" != success ||
        ( -n "$expected_title" && "$title" != "$expected_title" ) ]]; then
    echo "Expected a successful ${expected_title:-CodeQL} push run at $head_sha. See the PR checks." >&2
    exit 1
  fi
}

require_passed_run remote-quality.yml "Quality pre-release · $branch"
require_passed_run codeql.yml ""

behind_main="$(gh api "repos/$repository/compare/main...$head_sha" --jq '.behind_by')"
if [[ "$behind_main" != 0 ]]; then
  echo 'The release candidate must include current main. Update the branch and verify the new SHA.' >&2
  exit 1
fi

gh pr merge "$pr_number" --repo "$repository" --merge --match-head-commit "$head_sha"
