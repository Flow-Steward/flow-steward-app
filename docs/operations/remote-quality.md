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
4. Before merge, push the same clean candidate SHA to a task-specific verification
   ref (commands below). `verify-release` runs cumulative Tier 2 + Tier 3;
   `verify-sonar` additionally runs the Sonar pilot for substantial core changes.
   The Run workflow menu's `release` / `release-sonar` modes are also available
   once the workflow exists on main.
5. Recheck origin/main immediately before merge. If it is no longer an ancestor
   of the verified candidate, integrate it and repeat verification on the new SHA.

From the **public source checkout**, using an existing Git write connection:

```bash
# Replace my-task with the task's own stable name; use one verification ref per task.
git push origin HEAD:refs/heads/codex/verify-release/my-task
# Or, for the full gate plus the Sonar pilot:
git push origin HEAD:refs/heads/codex/verify-sonar/my-task
```

These refs preserve the candidate SHA; the workflow checks that SHA and current
main before proceeding. Updating the same task ref cancels its obsolete run;
separate task refs run independently. No GitHub CLI token is needed for this
route. The agent must read the resulting exact-SHA run and await its conclusion.
Remove owned verification refs after merging while retaining the merged commit.

Manual dispatch requires the workflow to exist on main. Bootstrap is handled by
the dedicated `codex/remote-quality-pilot` branch: its push runs `release-sonar`
without merging unverified workflow changes into main. Ordinary branch pushes run
Tier 2 only; the two verification prefixes request the full modes. Latest pushes
cancel obsolete work on that same branch; different
branches get independent VMs. No pull_request_target or self-hosted runner is used.

The local pre-commit hook keeps environment-artifact synchronization and the
repository boundary guard, then runs affected backend/frontend Tier 1 checks.
The manual full hook and direct lint.sh commands remain available for diagnosis.

## Sonar pilot and rollout

The `sonar-diagnostic` dispatch mode measures the Sonar runtime independently:
Tier 2 runs first, Tier 3 is not selected, and Sonar analyzes all canonical sources
without test coverage. The bootstrap runtime-pilot branch selects this mode.
It cannot qualify a release; use `release-sonar` for cumulative release evidence.

The `parallel-diagnostic` mode measures frontend-tests and backend-tests on
separate standard VMs after Tier 2. Each VM reads its actual command from the
canonical check registry and runs it through lint.sh and fs-audit. Worker counts,
timeouts, and test scope are unchanged. Both jobs finish even if one fails.
This runtime comparison does not replace cumulative `--full`: cross-profile and
external acceptance checks, and final evidence validation still belong to the
release route. The bootstrap parallel-pilot branch selects this measurement mode.

The Sonar job starts a fresh, disposable SonarQube server, rotates the bootstrap
admin password and creates a masked per-run token. It runs `lint.sh --sonar-full`
with the existing profile and that same run's coverage. The backend and frontend
scope/exclusions are unchanged. Findings are exported before teardown.

For this pilot, Sonar also runs diagnostically after a completed failed quality
job, using only that same run/SHA's artifact. Failed-test coverage may be
incomplete. Missing artifacts stop analysis; cancelled or skipped quality jobs
do not launch it. The quality failure remains failed, so a successful diagnostic
scan cannot qualify the candidate for release.

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

The hosted overlay groups 128 files per ESLint invocation (the local default
remains 32). This keeps the same rules and complete source selection while
avoiding repeated TypeScript-program startup. Tier 2 records container CPU and
memory samples every 15 seconds to check the hosted memory budget. This grouping
is a performance pilot; compare its timings and peak samples before rollout.
