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
main before proceeding. Only the `release` and `release-sonar` modes fail when the
candidate does not include current main, at the start or because another pull
request landed during the run; the error names both commits. Tier 2,
`single-check` and the diagnostic modes keep their result and report a warning,
because it still describes the branch commit. Updating the same task ref cancels its obsolete run;
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

## Focused recovery after a failed gate

Save the completed failed run's URL, candidate SHA, full failure list and logs.
Group failures by cause, make one fix batch, then run the failed and affected
tests through the canonical wrappers. Compare the fixes with that failed SHA,
so a long feature branch does not broaden the fix cycle to every earlier edit:

```bash
# Substitute the actual failed commit and regression node IDs/paths.
./core/entrypoints/cli/scripts/lint.sh --checks targeted-tests \
  --test-paths "auto:<failed-sha> tests/unit/test_regression.py"
# Use the explicit union of failed and affected frontend test files.
./core/entrypoints/cli/scripts/lint.sh --checks frontend-targeted-tests \
  --test-paths "src/feature/failed.test.tsx src/consumer/affected.test.tsx"
```

Frontend `auto:<failed-sha>` is available on its own; it cannot be combined with
explicit paths in the current frontend wrapper. Add consumers outside the import
graph manually. These checks stop at the first failure and collect no coverage.
They neither run Tier 2/Tier 3 nor certify a release. Do not push each fix to a
`codex/verify-release/` or `codex/verify-sonar/` ref: those refs request a full run.
Do not use **Re-run failed jobs** for this cycle: one failed `quality` job owns
the whole cumulative gate, not just its failed test cases.

A later rerun of the same GitHub run/SHA can restore its previous attempt's
evidence and coverage artifact. The gate validates fingerprints and checksums;
only proven successful checks are reused. A missing artifact falls back to
execution. A failed frontend or backend check still reruns that check's whole
suite in full mode; this is not per-test recovery. A new commit does not inherit
commit-scoped backend passes from an older commit. An unchanged core frontend
can reuse its input-scoped pass across commits: all copied assets, lockfile,
gate/toolchain and relevant environment must match. Custom extension manifests
remain bound to the producing commit.

The optional dispatch `reuse_run_id` imports retained evidence from an explicitly
selected, completed official remote-quality run, including successful checks
within a failed run. Wrong provenance, missing/expired artifacts, or an invalid
explicit request stops the job before expensive preparation. Restoring an
artifact does not make any check pass: fingerprints, statuses, checksums and
scope still decide. Coverage is eligible for import only for the same SHA;
results from another candidate must not become current candidate coverage.
Changes to the registry, fingerprint implementation or common gate/toolchain
invalidate old fingerprints too. Importing evidence produced before such a
change is not a way to certify the new harness; expect cache misses. Establish
a new baseline separately from the current focused release exception.

### Hosted evidence reuse diagnostic

Dispatch `reuse-smoke` to prove the artifact/evidence path with one existing pure
evidence-policy regression in `fs-audit`. It creates only a separate diagnostic
check record, never a frontend/backend suite pass. Pass the producer run ID in
`reuse_run_id` for a second candidate with unchanged check inputs: the consumer
must reuse that record with zero runner calls and retain its original SHA. It
also compares the real frontend input fingerprint and assets digest across VMs
without executing the frontend suite. Missing evidence or a changed fingerprint
fails the diagnostic instead of silently running extra tests.

The bootstrap ref `codex/quality-reuse-live` forces this diagnostic even on push;
Tier 2, Tier 3, PostgreSQL image preparation and Sonar are excluded. Before the
dispatch mode is on main, a temporary `.github/quality-reuse-smoke-source.json`
containing `{"source_run_id":"<producer-id>"}` can select its consumer source.
That control file belongs only on the temporary proof ref and must not be merged
to main. The uploaded `reuse-smoke.json` explicitly records that this is not a
complete gate and no official suite result is claimed.

Official image publication can reuse an already successful cumulative remote
gate for the exact tagged SHA (ADR 0054), avoiding a second full run. A failed
gate cannot satisfy this route. An explicitly approved focused release exception
must be recorded separately with its final SHA, failed source run, actual test
selection/results and remaining coverage; it is not a full green gate.

## Sonar pilot and rollout

The `sonar-diagnostic` dispatch mode measures the Sonar runtime independently:
Tier 2 runs first, Tier 3 is not selected, and Sonar analyzes all canonical sources
without test coverage. The bootstrap runtime-pilot branch selects this mode.
It cannot qualify a release; use `release-sonar` for cumulative release evidence.

Release modes run Tier 3 on parallel VMs (ADR 0068). The `tier3` job runs four
parts after Tier 2 — backend, contracts with the cross-profile heartbeat, frontend,
external — each through `lint.sh --full --tier3-only`, with the Tier 2 evidence
and the PostgreSQL image the quality job built once for the run. Selected suites
run only the tests the change reaches since an earlier result. `release_gate`
then runs the cumulative `--full` over all parts' evidence and runs nothing again;
it is skipped when a part failed. `release-full` (and the Sunday schedule on main)
runs every selected suite whole and rebuilds the impact maps.

Every run imports the evidence and impact maps of the latest runs whose commit
is an ancestor of its own, whatever branch ran them. Fingerprints decide reuse as
before, so a commit that leaves a check's inputs unchanged does not run it again;
a retry after a failed part runs that part's failing modules and what the fix
reaches.

Measured on the 0.9.18 batch: 0.9.17's release run took about 90 minutes with
Tier 3 serial on one VM. With parallel parts a whole run takes about an hour, the
contracts part (25,000 tests) being the longest; a run after a change that leaves
`assets/` alone reuses Tier 2 in under a minute and the frontend suite entirely.

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

For a secret-scanner measurement without a full quality tier, dispatch mode
`secret-scan-only`. The dedicated `codex/secret-scan-live` measurement ref always
selects only that scanner job, including when an older dispatch form still shows
`tier2`. It cannot select the normal quality, Tier 3 or Sonar jobs.
The first run with an empty `reuse_run_id` starts with no per-file passes. After
it completes, dispatch the same ref with its run ID in `reuse_run_id`; this mode
validates and imports only the exact source's dedicated scanner manifest.
An invalid explicit source fails rather than silently starting cold. Read
`secret-scan.json` for scanned/reused file counts, source identity and elapsed
scanner-command time; image preparation is separate. These measurements create
no whole-tier or release evidence. A changed scanner, baseline or toolchain
requires new file results; changing a commit SHA alone does not.

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
