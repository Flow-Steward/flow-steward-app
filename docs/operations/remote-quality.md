# Remote quality checks

The public source is `Flow-Steward/flow-steward-app`. Tier 1 stays on the
developer's computer. The Remote quality pilot workflow runs the same canonical
Docker gate on standard hosted Linux runners. It neither publishes nor merges.

## Agent workflow

1. Run local affected Tier 1 checks while editing.
2. Before a long gate, fetch origin and merge current origin/main into the branch.
   Resolve conflicts, rerun affected Tier 1 checks, then commit and push the public
   candidate. Do not push private repository history or secrets to the public repo.
3. Every branch push runs Tier 2. Read the run for that exact SHA and wait for its
   conclusion; a pending, skipped or failed run is not verification.
4. Before merge, select `release` in Remote quality pilot's Run workflow menu on
   the candidate branch. It runs cumulative Tier 2 + Tier 3. For substantial core
   changes select `release-sonar` for the full gate followed by the Sonar pilot.
5. Recheck origin/main immediately before merge. If it is no longer an ancestor
   of the verified candidate, integrate it and repeat verification on the new SHA.

Manual dispatch requires the workflow to exist on main. Bootstrap is handled by
the dedicated `codex/remote-quality-pilot` branch: its push runs `release-sonar`
without merging unverified workflow changes into main. Other branch pushes run
Tier 2 only. Latest pushes cancel obsolete work on that same branch; different
branches get independent VMs. No pull_request_target or self-hosted runner is used.

The local pre-commit hook keeps environment-artifact synchronization and the
repository boundary guard, then runs affected backend/frontend Tier 1 checks.
The manual full hook and direct lint.sh commands remain available for diagnosis.

## Sonar pilot and rollout

The Sonar job starts a fresh, disposable SonarQube server, rotates the bootstrap
admin password and creates a masked per-run token. It runs `lint.sh --sonar-full`
with the existing profile and that same run's coverage. The backend and frontend
scope/exclusions are unchanged. Findings are exported before teardown.

An empty server does not have the existing server's project history or new-code
baseline. Keep the existing Sonar release check until quality-profile/gate parity
and baseline behavior are reconciled. A pilot failure is reported as a failure,
not retried under weaker thresholds. Hosted CPU/memory/disk suitability must be
measured before declaring the transition complete.

## Disk and retained data

Audit/PostgreSQL image layers use separate BuildKit GitHub cache scopes. Only npm
downloads are restored as package cache; passed evidence is never imported from
an Actions cache. Cache misses are allowed and cannot produce a synthetic pass.
GitHub's cache quota/eviction applies; do not increase paid storage limits for
this pilot. Docker's duplicate local cache export is unused when the prebuilt
validated audit image is present.
The audit cache retains intermediate frontend stages so a backend-only change
does not reinstall frontend dependencies and recompile TypeScript. Output
ownership is returned to the host before uploading private-mode evidence files.

Each job removes its exact Compose project and disposable volumes with an always
step. Its VM, images and BuildKit state are discarded after the job, including
forced termination. The shared developer Docker daemon, databases, images and
volumes are never involved. Diagnostic artifacts expire after three days; keep
only the minimal reports needed for diagnosis or release evidence elsewhere.

The workflow records gate output, evidence, coverage and Docker/disk diagnostics.
Compare cold versus warm runs and separate preparation, Tier 2, Tier 3 and Sonar
time before changing worker counts or buying a larger machine.
