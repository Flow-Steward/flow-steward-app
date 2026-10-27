# ADR 0037: Parallel Backend Unit Suite with Per-Worker Audit Databases

## Status

Accepted — 2026-10-15.

## Context

The Tier 3 backend unit suite (`lint.sh --checks tests --quality-owner-tier 3`)
ran about 18,500 tests in one pytest process and took 45 to 50 minutes. It had to
stay serial for one reason: every test shared the gate's disposable database,
`flow_steward_audit` on `pg-audit`, and the copied workspace. Concurrent workers
on one database would see each other's rows, deletes and canonical-seed changes,
so a parallel run could pass or fail depending on scheduling.

## Decision

1. `lint.sh` runs the whole unit suite on a fixed number of pytest-xdist workers
   (`-n 4 --dist loadgroup`). The count is fixed so runs are comparable and so
   the peak memory stays inside the shared Docker VM. The suite still runs
   without `-x`; coverage from every worker is combined by pytest-cov into the
   same `coverage.xml` and `coverage.txt` the gate exports, and the
   skipped-test rule reads that same report.
2. Isolation is per worker, not per test. Before any product module is imported,
   `tests/_xdist_isolation.py` clones the initialized `flow_steward_audit` into
   `flow_steward_audit_<worker>` (`CREATE DATABASE ... TEMPLATE`, serialized by
   an advisory lock) and points every DSN variable at it. Each worker starts from
   the canonical state a serial run starts from and runs its share of the tests
   in order. The gate's local artifact roots get a per-worker subdirectory.
   Nested pytest runs inherit the redirect and never recreate their parent's
   database.
3. The fail-closed audit guards accept exactly `flow_steward_audit` and the
   current worker's copy; nothing else on `pg-audit` and nothing off it.
4. Every worker must collect the identical test list. The workers share one
   `PYTHONHASHSEED`, drawn per run and printed, so a set-ordered parametrization
   cannot split them; the test itself is still fixed to a sorted order.
5. Modules that share an expensive per-process cache (the architecture scans'
   parsed source index, the env-audit scan) are grouped onto one worker by
   `tests/unit/conftest.py`, so the cache is paid once, not once per worker.
6. A test that cannot be isolated is pinned with
   `@pytest.mark.xdist_group("serial")`: `loadgroup` runs the whole group on one
   worker, in order.
7. Tier 1 `targeted-tests` stays single-process and fail-fast; outside an xdist
   worker the isolation code does nothing. pytest runs unbuffered (`-u`) so a run
   the kernel kills for memory still shows how far it got.

## Consequences

- Measured on 2026-10-15 on the same commit with an otherwise idle Docker VM
  (19,432 tests, all passing either way, the same coverage total):
  serially 41:36 of pytest time at a 2.6 GB peak; on four workers 11:24
  (3.6 times faster) at a 6.7 GB peak for all test processes (each worker about
  1.1 GB, the source-scan worker about 1.7 GB, the controller about 1.8 GB while
  it combines coverage); on six workers 9:33 at 8.4 GB. Four keeps most of the
  gain for far less memory. With other stands holding most of the VM, a
  four-worker run took 47 minutes and earlier attempts were killed by the VM's
  OOM killer, so the gate needs about 7 GB of headroom.
- The broker needs no per-worker vhost: no unit test opens a real RabbitMQ
  connection (the vhost held no queues, exchanges or connections during the
  run). A test that starts using the real broker has to bring its own isolation
  or join the serial group.
- An order dependence between test files is now a defect that can surface as a
  parallel-only failure; it is fixed in the test, not retried. The first runs
  found two: a module that switched the process to enforce mode at import, and a
  config reload test that left its last globals behind.
- Each worker collects the whole suite, so collection (several minutes under
  coverage) is paid in parallel rather than once; it is the floor of the run.
- `pytest-xdist` (and `execnet`) join the audit image through
  `requirements-audit.txt`, which is already an input of every fingerprint.
