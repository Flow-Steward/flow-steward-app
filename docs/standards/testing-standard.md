# Testing Standard

This document defines the test policy for the Flow Steward repository.

## Purpose

The goal of this standard is to keep tests trustworthy, high-signal, and aligned with real system behavior.

## Testing Philosophy

- `MUST` treat tests as contract verification, not only as execution smoke.
- `MUST` prefer exact behavior checks over permissive assertions.
- `MUST` write the cheapest test that proves the required behavior.
- `SHOULD` keep most verification at unit and integration level.
- `SHOULD` use end-to-end tests for cross-runtime, UI, or environment-sensitive flows that lower layers cannot prove well.

## Test Layout

The canonical test layout is:

- `tests/unit`
- `tests/integration`
- `tests/e2e`
- `tests/ui`
- `tests/security`
- `tests/db`
- `tests/fixtures`

Rules:

- `MUST` place tests in the matching layer for the behavior under proof.
- `SHOULD` keep domain-specific fixtures close to the domain.
- `SHOULD` keep shared fixtures small and stable.

## Required Tests by Change Type

- Bug fix: `MUST` add a regression test that fails without the fix.
- New business logic: `MUST` add unit or integration coverage for happy path and error path.
- New or changed endpoint: `MUST` verify status, shape, and critical response behavior.
- Security-sensitive change: `MUST` add security-oriented verification.
- UI-critical JavaScript change: `MUST` verify the affected runtime path without console or page errors.
- DB migration: `MUST` verify migration behavior at the database layer.
- Cross-runtime workflow change: `SHOULD` add integration or e2e verification where unit tests cannot prove the contract.

## Assertion Quality

- `MUST` assert the expected contract, not only broad success.
- `MUST` avoid “not 500” as the main assertion for important flows.
- `MUST` avoid broad accepted status ranges unless the range is the intended contract.
- `SHOULD` assert key payload fields, state transitions, and side effects explicitly.
- `SHOULD` make failure messages easy to interpret.

## Test Shape

- `SHOULD` use an Arrange / Act / Assert shape so the contract under proof is easy to review.
- `SHOULD` keep each test centered on one behavior and one main reason to fail.
- `SHOULD` use parametrization when the contract is identical across roles, inputs, or transport variants.

**Example — Arrange / Act / Assert:**

```python
def test_delete_project_requires_admin(client, admin_headers, seeded_project):
    # Arrange
    project_id = seeded_project["id"]

    # Act
    response = client.delete(f"/api/projects/{project_id}", headers=admin_headers)

    # Assert
    assert response.status_code == 204
```

**Example — parametrized pytest contract:**

```python
@pytest.mark.parametrize(
    ("role", "expected_status"),
    [("viewer", 403), ("operator", 200), ("admin", 200)],
)
def test_policy_update_role_matrix(client, role, expected_status):
    response = client.post("/api/projects/1/policy", headers=headers_for(role), json={"mode": "manual"})
    assert response.status_code == expected_status
```

## Fixtures and Mocks

- `MUST` keep tests isolated and repeatable.
- `MUST` mock external I/O when the goal is not to test the external dependency itself.
- `MUST` avoid mocking the main subject under test.
- `SHOULD` use parametrization instead of duplicating near-identical tests.
- `SHOULD` keep fixtures purposeful and readable.

## Async and Runtime Tests

- `MUST` test async behavior with the correct async test primitives.
- `MUST` use async-aware mocks and fixtures for async boundaries.
- `SHOULD` keep runtime and worker tests deterministic.
- `SHOULD` prove retry, failure, and degraded-mode behavior where it matters operationally.

## Verification Tiers

Each executable quality check has one owning tier in
`infra/tooling/quality/check-registry.json`. Higher tiers execute their delta;
they do not rerun checks owned by lower tiers. A matching fingerprint may reuse
checksummed evidence. Tiers 1–3 run in local Docker; final verification must
identify the exact commit SHA and candidate environment. GitHub Actions are
disabled, and no GitHub CI artifact or remote status check is required.

Evidence has one of two reuse scopes, set per check in the registry (ADR 0036):

- `commit` (the default, and every Tier 3 suite): a result counts only for the
  commit that produced it.
- `inputs`: a passed result counts for any commit whose fingerprint for that
  check matches. The fingerprint covers the check's declared inputs, the gate
  tooling and the audit image inputs, so a check `MUST` be marked `inputs` only
  when its `inputs` name every file its tool reads.

Unchanged work is never repeated: a commit that touches only the frontend does
not rerun mypy, a later commit that leaves `core/` alone does not rerun Semgrep.

### Tier 1: Iteration

- `MUST` reproduce a bug with a regression test and observe the expected failure before applying the fix.
- `MUST` run the regression test, tests owned by the changed module, and direct consumers identified through dependency or blast-radius analysis.
- `SHOULD` derive that selection with `lint.sh --checks targeted-tests --test-paths auto` and `lint.sh --checks frontend-targeted-tests --test-paths auto`: changed tests, tests importing a changed module or one of its direct importers, tests that name a changed non-Python file, and every vitest test that imports a changed frontend source. Tests the import graph cannot see (runtime, database, extension) are added by path.
- `MUST` add the specialized checks required by the change type, including API contract, database, security, frontend runtime, workflow runtime, or extension verification where applicable.
- `MUST` use fail-fast behavior such as `pytest -x` or `--maxfail=1` for sequential test groups.
- `MUST NOT` collect coverage during the edit/fix loop unless coverage itself is under test.
- `SHOULD` use `pytest --lf -x` when the previous failures remain relevant to the current code.

Tier 1 proves local progress; it does not prove that the complete change is ready for handoff.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.

### External extension checkouts

A standalone extension repository is checked by the same gate, never by a host
interpreter and never by a core-only result:

```bash
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root /abs/path/to/extension-checkout
./core/entrypoints/cli/scripts/lint.sh --checks extension-tests --extension-root /abs/path/to/extension-checkout --test-paths "tests/test_a.py tests/test_b.py"
```

- `extension-static` runs the bundle's `scripts/validate_bundle.py` (when present), `compileall`, `ruff check`, `ruff format --check`, `mypy` and `bandit` over the bundle's own modules, with the gate image's tool versions and the bundle's own `pyproject.toml` configuration. Every step runs and each failure is reported.
- `extension-tests` is the `extensions test` path: manifest validation, then pytest in a venv installed offline (`pip --no-index`) from the bundle's `dev-wheels/` and `wheels/`. Selected `--test-paths` (relative to the extension root) run fail-fast with `-x`; without a selection the whole `tests/test_*.py` suite runs to the end.
- Neither check reaches the network, so neither runs `pip-audit`; a dependency audit of an extension is a separate, explicit step.
- `--extension-id` is optional and must match `extension.yaml`. The checkout is staged in the container under its canonical folder name (`acme.crm-sync` → `acme_crm_sync`).
- The checkout is never bind-mounted: the host streams it into `fs-audit` as a tar archive (without `.git` and tool caches), because Docker Desktop serves stale files through a bind mount once a mounted file changes. The container cannot write back. The log names the checkout's commit and its count of uncommitted paths; extension evidence `MUST` cite that commit and a clean tree.
- Both checks are owned by Tier 1 with `selection_required`: they need an input no tier run has, so `--fast` and `--full` never select them, and a green core gate is not extension evidence.

### Tier 2: Intermediate Commit

- `MUST` run `./core/entrypoints/cli/scripts/lint.sh --fast` before an intermediate commit.
- `MUST` start checks in registry order: `pip-audit` and the frontend dependency audit, then `detect-secrets` and Semgrep, then the remaining static checks. Up to four checks run at once, each in its own temporary workspace, and each check's log is printed as one block.
- `MUST` start no further Tier 2 check after the first failure; checks already running finish, and every completed check retains real fingerprinted evidence. A deferred or skipped implementation `MUST NOT` record a pass.
- A scanner that judges each file on its own (detect-secrets) `MAY` skip file contents it already passed under the same tool version and configuration. A cache that cannot answer `MUST` fall back to scanning every file.
- `MAY` omit the full suite when Tier 1 covers the affected behavior and the fast gate passes.
- `MUST` review and stage regenerated tracked artifacts explicitly; hooks `MUST NOT` run hidden `git add` operations.

### Tier 3: Handoff, Pull Request, or Merge

- `MUST` run one clean `./core/entrypoints/cli/scripts/lint.sh --full` after a coherent change batch is complete.
- The canonical `--full` wrapper `MUST` validate or refresh commit-bound Tier 2 evidence before starting long Tier 3 test infrastructure, then execute only the Tier 3-owned delta.
- `--full` `MUST` finish by validating complete Tier 2 and Tier 3 evidence against the same final SHA; it `MUST NOT` report success when either tier is missing, failed, corrupt, stale, or bound to another command or fingerprint.
- The backend unit suite `MUST` run without `-x` or `--maxfail=1`, retain the complete failure list, and report failures grouped by root cause before one coherent in-scope fix batch.
- `MUST` bind the result to the final commit being handed off: commit-scoped checks through that SHA, input-scoped checks through a fingerprint computed from that SHA's tree. Any later change to a check's inputs invalidates that check.
- `MUST` run Tier 3 before handoff for database schema, authentication or authorization, security boundary, shared store or port, worker/runtime, extension framework, or deployment configuration changes regardless of diff size.

### Release verification

- `MUST` require complete green Tier 2 and Tier 3 evidence for the exact final commit, with the current registry ownership, fingerprints, and commands: commit-scoped evidence recorded for that SHA, and input-scoped evidence whose fingerprint matches that SHA's tree.
- `MUST NOT` treat the Tier 3 delta alone as proof of Tier 2 or publish when the cumulative `--full` validation fails.
- `MUST` run integration/E2E and candidate smoke checks when required by the changed runtime contract, recording image digests, compose project, and environment.
- Additional deep audit checks `MAY` be selected explicitly with `lint.sh --checks`; they are not a fourth tier or a GitHub prerequisite.

## Test Selection

Tier 1 selection `MUST` include, in order:

1. the regression test proving the defect or contract;
2. the owning module or package tests;
3. direct callers, consumers, and affected transport variants;
4. broader verification for shared contracts or runtime boundaries;
5. specialized suites required by the change type.

A small diff `MUST NOT` be treated as low risk solely because it is small.

## Failure and Retry Semantics

- During Tier 1 focused iteration, a deterministic failure `MUST` stop its current sequential group immediately.
- The Tier 3 backend unit suite is the explicit exception: it `MUST` finish collecting its failure list before dependent checks stop; the failures are then grouped by shared root cause and fixed as one justified in-scope batch.
- Tier 3 `MUST` run every owned check to the end even after one fails, so one run reports the whole batch; only a check whose prerequisite failed is skipped and reported as blocked.
- Infrastructure and harness failures `MUST` be classified separately from product failures, and dependent checks `MUST` stop when their prerequisite environment is invalid.
- A failed test `MUST NOT` be rerun repeatedly without a stated hypothesis or evidence of an external transient failure.
- A quarantined flaky test `MUST` have a tracked issue, owner, removal deadline, and explicit exclusion from release-coverage claims.
- Results produced after shared test state or the environment becomes invalid `MUST NOT` be counted.

## Local Execution Policy

- `MUST` run quality gates and SonarQube locally in Docker.
- `MUST NOT` require GitHub Actions, GitHub artifacts, or remote check status for handoff or merge.
- Critical skipped or excluded tests `MUST` remain visible as temporary debt.
- Deterministic configuration and generated-artifact checks `SHOULD` run before expensive tests.
- Coverage `SHOULD` be collected once by the full gate, not during Tier 1.
- Gate caches (shared evidence, per-file scan results, the npm package cache) live in the `flow-steward-quality-cache` volume that `fs-audit` mounts for every checkout and compose project. Every entry is content-addressed, so a fresh worktree reuses them; removing the volume only makes the next run cold.
- The Tier 3 backend suite reports its slowest tests; a test that dominates the run `SHOULD` be made cheaper before it is moved into a serial group.
- Final gates `MUST` verify committed content and `MUST NOT` be replaced by a dirty local result.
- Repeated runs `MUST` have a distinct contract, relevant change, or failure hypothesis.

## Parallel Backend Suite

The whole backend unit suite (`lint.sh --checks tests`, Tier 3) runs on a fixed
number of pytest-xdist workers with `--dist loadgroup` (ADR 0037). Tier 1
`targeted-tests` stays single-process and fail-fast.

- The worker count is fixed in `lint.sh`, not taken from the machine, so two runs are comparable. Changing it is a gate change and `MUST` be measured for wall time and peak memory first.
- Each worker `MUST` run against its own copy of the audit database. `tests/_xdist_isolation.py` clones `flow_steward_audit` into `flow_steward_audit_<worker>` before any product module is imported and points `FS_DB_DSN`, `DATABASE_URL`, `LANGGRAPH_POSTGRES_URI` and `POSTGRES_DB` at it; the gate's local artifact roots get a per-worker subdirectory. A test that asserts which audit database it writes to `MUST` compare against `audit_database_name()`, never the literal name.
- Outside an xdist worker, or outside the isolated gate, the isolation is a no-op: a serial run behaves exactly as before.
- Tests `MUST NOT` depend on another test file having run first, on running in the same process as another file, or on a fixed TCP port, a fixed path under the shared workspace or `/tmp`, or a fixed queue or exchange name. Use `tmp_path`, port `0` and generated names.
- Every worker `MUST` collect the same tests in the same order. Parametrize over a list or a sorted sequence, never a set: `lint.sh` gives all workers one `PYTHONHASHSEED` per run (printed in the log) so a set-ordered id list cannot split them, but the sorted form is the fix.
- A test module `MUST NOT` change shared module state at import time (for example assigning a config module's attribute at top level) or leave a reload's globals behind. Collection imports every module in every worker, and which later test happens to restore the state differs per worker. Use `monkeypatch` or a fixture that restores what it changed.
- Modules that share an expensive per-process cache (the parsed source index of the architecture scans, the env-audit scan) are kept on one worker by the `_SHARED_CACHE_GROUPS` table in `tests/unit/conftest.py`, so the cache is built once rather than once per worker. A new module that reads one of those caches belongs in its group.
- A test that cannot be isolated this way `MUST` be marked `@pytest.mark.xdist_group("serial")` with a comment naming the shared resource. Every test in that group runs on one worker, in collection order, while the other workers continue. The group is a last resort with a reason, not a fix for an order-dependent test.
- A failure that appears only in the parallel run is a test-isolation defect. It `MUST` be reproduced (the single-process run of the same files is the reference) and fixed in the test, not retried.

## Verification Evidence

- Evidence reuse `MUST` match check ID, owner tier, input fingerprint, runtime identity, status, and required trust level.
- `pip-audit` acceptance evaluation and evidence fingerprints `MUST` use the same UTC calendar day. An acceptance remains valid through its documented expiry date; a new UTC day invalidates prior-day `pip-audit` evidence even for the same commit. Callers `MUST NOT` override this clock identity, and the registry `MUST` fingerprint the acceptance evaluator source.
- `pip-audit` `MUST` audit every lock file a product image installs exactly as pinned (hashed, no resolution), in addition to resolving the unpinned requirements; a lock added to `infra/docker/Dockerfile` `MUST` join `product_locks` in `run_pip_audit`. Findings are labelled by lock (`runtime-lock`, `compact-lock`, …) and accepted per label like any other.
- Missing, corrupt, failed, incomplete, or infrastructure-failed evidence `MUST NOT` be reused as a pass.
- `--evidence-mode off` `MAY` be used for diagnosis, but its results `MUST NOT` be presented as retained reusable release evidence.
- Completion reports `MUST` state exact commands, pass/fail/skip/unverified results, commit SHA, runtime candidate identity, omitted checks and reasons, and the achieved tier.
- Results from different commits, images, compose projects, databases, or candidate environments `MUST NOT` be combined as one green run. Input-scoped evidence is the one exception: its fingerprint, recomputed from the final tree, proves it describes the same inputs and tools.
- A targeted green run `MUST NOT` be reported as a green full gate.
- Test ordering and shard boundaries `SHOULD` be reviewed after 20 to 30 representative local runs using time to first failure, total time to green, shard duration, flaky rate, and repeated-test percentage.

## Validator/Gate ↔ Consumer Parity

- Every validator or gate `MUST` have parity coverage against its real consumer.
  A portable bundle accepted by validation must remain valid at import, and the
  persisted workflow must retain the topology, contracts and step configuration
  inspected by that validation.
- A systematic authoring failure `MUST` first trigger deterministic validator,
  import and persistence parity checks before changing model guidance.
- Successful creation `MUST` be proven through validation, persistence and
  inspection of the actual stored workflow. A proposed plan, validation report
  or success response alone is insufficient evidence of creation.
