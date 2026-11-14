# ADR 0068: Impacted Tier 3 selection and a parallel hosted release gate

- Status: Accepted
- Date: 2026-11-14
- Amends: [ADR 0053](0053-hosted-quality-gates.md) (complete suite scope of the
  hosted cumulative gate, serial Tier 3), [ADR 0036](0036-content-scoped-quality-evidence.md)
  (evidence carried between hosted runs)

## Context

A hosted `release-sonar` run took 65–90 minutes (run 43383269232: Tier 2 7.5 min,
frontend-tests 16 min, then backend 25,037 tests 30 min, externals 6 min, Sonar
after all of it). Three causes, none of them the size of a change:

1. Content-scoped reuse (ADR 0036) lives in the local `flow-steward-quality-cache`
   volume. A hosted run starts on an empty VM and imported evidence only from an
   earlier attempt of the same run or an explicit `reuse_run_id`; every new commit
   ran every Tier 2 and Tier 3 check cold, including the retry after a one-line fix.
2. A Tier 3 suite was all-or-nothing: any Python change ran all backend tests.
3. Tier 3 ran its suites one after another on one VM.

The import graph alone cannot narrow the backend suite: direct importers miss
tests that reach a module through the application factory, and the transitive
closure selects more than half of the suite for a one-file change, because
almost every test builds the whole application.

## Decision

**Evidence travels between hosted runs.** The quality job imports the evidence
and impact maps of the latest runs of the same branch, then main
(`hosted_evidence_source.cjs`). Imported records are candidates: fingerprints still
decide reuse, so a check whose inputs did not change is not run again on another
commit, and nothing is accepted that the policy would not accept locally.

**Tier 3 suites run the tests a change reaches.** A check may declare a
`selection` in the registry. When its evidence does not match, verify.py plans:

- The base is the closest ancestor commit with a result of the same check, the
  same command and the same runtime digest (runtime identity without disposable
  image IDs): a passed result, or a failed one whose failing modules are known.
  Without such a base, or with `--full-suite`, the suite runs whole.
- `pytest-impact` (backend-tests, backend-frontend-contracts): a pytest plugin
  (`impact_recorder`, `sys.monitoring` plus audit hooks) records per test module
  the repository files whose functions it ran and the files it opened, listed or
  launched. Code that runs before a module's tests — collection, session and
  package fixtures — is recorded as shared. A test module is selected when it
  changed, used a changed file, or imports a module whose module level changed
  (following importers no test used up to used ones); failing modules of the base
  run again; a non-Python file also selects tests naming it. Gate tooling, declared
  `full_run_paths`, removed modules, shared files and the root conftest run the
  whole suite, as does a selection above 60% of the suite. A full run replaces the
  map, a selected run refreshes the modules it ran.
- `vitest-related` (frontend-tests): `vitest related` on the changed frontend
  sources and the base's failing test files; any other input change runs it whole.

The evidence record names the base (`selection_base_sha`), the selected module
count, the failing modules and the runtime digest. A selected pass is the gate's
Tier 3 result for that SHA: it vouches for the selected tests directly and for the
rest through its base. The chain always ends in a whole-suite run.

**Tier 3 runs on parallel VMs.** After Tier 2 the `tier3` job runs four parts
(backend, contracts with the cross-profile heartbeat, frontend, external) through
`lint.sh --full --tier3-only`. `release_gate` then runs the cumulative `--full`
over their evidence, which finds every check reusable and runs nothing again.
Sonar runs beside it with the parts' coverage. The PostgreSQL image is built once
per run and handed to every job, because backend evidence binds its image ID.

**A weekly whole-suite run is the backstop.** On Sundays the workflow runs main in
`release-full` mode: every selected suite whole, maps rebuilt. `release-full` is
also available on demand.

## Consequences

- A fix after a failed run reruns the failing modules and what the fix reaches, not
  the batch. A docs-only change runs no backend test at all.
- A selection can miss what neither the map nor the import graph sees, for example
  a behaviour change that reaches a test only through data built at import time by
  an untouched module. Such a miss surfaces in the weekly run, not in the release.
  The owner accepted this trade on 2026-11-14 for release throughput; a release that
  needs complete coverage dispatches `release-full`.
- Sonar coverage now reflects the executed scope of each part; on a selected run it
  is partial. The pilot gate does not fail on coverage (ADR 0053).
- Evidence of a parallel part is ordinary check evidence; a part never runs the
  release read, and `release_gate` refuses a run whose parts did not all succeed.
