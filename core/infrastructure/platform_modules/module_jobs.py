"""Periodic background jobs that platform modules run in the control role (ADR 0082).

A module's ``register_control`` hook schedules a job with
``context["schedule"](name, interval_seconds, fn, timeout_seconds=None)``. The
control process runs every job on its own scheduler thread:

- **Cadence.** The first run comes after a random delay of up to one tenth of the
  interval (at most 60 seconds); each later run comes one interval plus such a
  delay after the previous start, so replicas and jobs do not fire in step. The
  interval is at least :data:`MIN_JOB_INTERVAL_SECONDS`. No run starts while
  maintenance is active; a due job then waits for it to end.
- **Maintenance.** The signal is the one Core's other control loops and the worker
  use to stop taking work (``public_submission_pause_active``): the lifecycle pause
  marker or the maintenance marker under ``/run/flow-steward-runtime``. A run that is
  going when maintenance starts is asked to stop (``ctx["should_stop"]()`` turns
  true). On Compact the control role runs in the one runtime container that owns
  that directory; on Scalable every ``control-plane`` container mounts the same host
  directory (``.flow-steward/runtime``, read-only), so each control replica sees the
  marker the moment the operator CLI writes it. An unreadable marker counts as
  maintenance (fail closed).
- **Single flight.** A job never runs twice at once in one process: a run that is
  still going when the job is due again is not joined by a second one. Across
  control replicas the job runs only on the replica that holds its PostgreSQL
  advisory lock (:class:`AdvisoryJobLease`): the lock is taken on a dedicated
  session and kept, so the same replica keeps running the job; when its session
  ends (the replica stops or loses its connection) another replica takes the lock
  at its next due time. Before each run, and every :data:`LOCK_KEEPALIVE_SECONDS`
  while a run is going, the replica checks in ``pg_locks`` that its session still
  holds the lock; a job whose lock is gone is not started, and a running one is
  asked to stop and recorded as failed.
- **Stopping.** :meth:`ModuleJobScheduler.stop` asks every running job to stop,
  waits for each up to the rest of its timeout and only then closes the lock
  session, so no other replica starts a job while this one still runs it.
- **Timeout.** A run that takes longer than its timeout (the interval by default)
  is recorded as failed and asked to stop: ``ctx["should_stop"]()`` turns true. A
  Python thread cannot be killed, so the job must check it in long loops; until
  the run returns, no new run of that job starts.
- **Failures.** An exception, a timeout or an account that failed in
  ``ctx["for_each_account"]`` is logged with the module id, version and job name
  and listed by :meth:`ModuleJobScheduler.failures` for the role's heartbeat, so
  administrators see it under the ``platform_modules`` monitoring check. The next
  clean run clears it. A failing job never stops the scheduler or the control loop.
- **Rollback.** A module whose loading fails loses every job it scheduled
  (``loading_checkpoint`` / ``restore_loading_checkpoint``); after loading the
  scheduler is sealed and no job can be added.

The job receives a read-only context: the control role's module ports, ``role``,
``platform_module`` (``{"id", "version"}``), ``job`` (its name), ``should_stop``
and ``for_each_account(fn, *, feature=None)``, which calls ``fn(account_id)`` for
every active account (with ``feature``, only for the accounts the role's
``entitlement_service`` says are entitled to it), logs and counts each account that
raises and carries on with the next, and returns ``{"processed": n, "failed":
[account ids], "skipped": n}``. Ports that are bound to a module identity (Core's
secret codec, ADR 0082 §12) are bound to the job's module.

Only the standard library and the loader's failure reason helper are imported:
the scheduler is technical runtime support and holds no business rules.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from core.infrastructure.platform_modules.loader import (
    bind_platform_module_services,
    module_failure_reason,
)

#: The shortest interval a module job may ask for.
MIN_JOB_INTERVAL_SECONDS = 60
#: The upper bound of the random delay added to every start.
MAX_JOB_JITTER_SECONDS = 60.0
#: The status a job failure carries in the role's module failure records.
MODULE_JOB_FAILED_STATUS = "job_failed"
#: How often the scheduler thread looks for due jobs and overdue runs.
DEFAULT_TICK_SECONDS = 1.0
#: How often a running job's lock is checked to be still held.
LOCK_KEEPALIVE_SECONDS = 30.0
#: How long :meth:`ModuleJobScheduler.stop` waits for the scheduler thread itself.
_SCHEDULER_JOIN_SECONDS = 5.0

_JOB_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_LOCK_PREFIX = "flow-steward:platform-module-job"

_logger = logging.getLogger(__name__)

JobFn = Callable[[Mapping[str, Any]], Any]


class ModuleJobError(ValueError):
    """A module asked for a job Core cannot schedule."""


class AdvisoryJobLease:
    """Cross-replica ownership of module jobs through PostgreSQL advisory locks.

    One dedicated session per process holds a session-level
    ``pg_try_advisory_lock(hashtextextended(<key>, 0))`` for each job this replica
    runs, the same lock primitive Core's extension operations use. The lock is
    kept between runs, so exactly one replica runs a job; PostgreSQL releases it
    when the session ends. A key this process took is confirmed in ``pg_locks``
    (granted to this session's backend) on every later call, so a session that
    silently lost its locks (a pooler handing out another backend, an
    ``pg_advisory_unlock_all``) is noticed. A failed query or a lost lock drops the
    session (and with it every lock), and the next call reconnects.
    """

    def __init__(self, connect: Callable[[], Any], *, logger: logging.Logger | None = None) -> None:
        self._connect = connect
        self._connection: Any = None
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._log = logger or _logger

    def hold(self, key: str) -> bool:
        """Whether this process holds ``key`` now, taking it when it is free."""
        with self._lock:
            row: Any = None
            lost = False
            try:
                if self._connection is None:
                    self._connection = self._connect()
                    self._held.clear()
                with self._connection.cursor() as cursor:
                    if key in self._held:
                        # Confirm the session still holds it, not only that it answers.
                        cursor.execute(_LOCK_HELD_SQL, (key,))
                        if _scalar(cursor.fetchone()) is True:
                            return True
                        lost = True
                    else:
                        cursor.execute(
                            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)
                        )
                        row = cursor.fetchone()
            except Exception as exc:
                self._log.warning(
                    "Platform module job lock session failed; it reconnects at the next due "
                    "time: %s",
                    type(exc).__name__,
                )
                self._drop()
                return False
            if lost:
                self._log.warning(
                    "Platform module job lock session no longer holds %s; it reconnects "
                    "at the next due time",
                    key,
                )
                self._drop()
                return False
            if _scalar(row) is True:
                self._held.add(key)
                return True
            return False

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        self._held.clear()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                self._log.debug("Platform module job lock session did not close cleanly")


#: Whether this session's backend holds the session-level advisory lock on a key: a
#: bigint key is kept as ``classid`` (high 32 bits) and ``objid`` (low 32 bits) with
#: ``objsubid = 1``.
_LOCK_HELD_SQL = (
    "WITH k AS (SELECT hashtextextended(%s, 0) AS v) "
    "SELECT EXISTS (SELECT 1 FROM pg_locks, k WHERE locktype = 'advisory' "
    "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
    "AND classid = ((k.v >> 32) & 4294967295)::oid "
    "AND objid = (k.v & 4294967295)::oid)"
)


def _scalar(row: Any) -> Any:
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0] if isinstance(row, (tuple, list)) and row else row


def postgres_connector(dsn: str) -> Callable[[], Any] | None:
    """A factory of autocommit PostgreSQL sessions on ``dsn``, ``None`` without one."""
    resolved = str(dsn or "").strip()
    if not resolved:
        return None

    def connect() -> Any:
        from core.infrastructure.db.psycopg_runtime import connect as psycopg_connect

        return psycopg_connect(resolved, autocommit=True)

    return connect


_ACTIVE_ACCOUNTS_SQL = "SELECT id::text FROM accounts WHERE is_active ORDER BY created_at, id"


def active_account_lister(connect: Callable[[], Any] | None) -> Callable[[], list[str]]:
    """List the ids of the active accounts on a short-lived session."""

    def list_accounts() -> list[str]:
        if connect is None:
            return []
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_ACTIVE_ACCOUNTS_SQL)
                rows = cursor.fetchall() or ()
        finally:
            connection.close()
        ids: list[str] = []
        for row in rows:
            value = row[0] if isinstance(row, (tuple, list)) else row
            if isinstance(row, Mapping):
                value = next(iter(row.values()), None)
            text = str(value or "").strip()
            if text:
                ids.append(text)
        return ids

    return list_accounts


@dataclass
class _Run:
    thread: threading.Thread
    started: float
    stop: threading.Event
    timed_out: bool = False
    lock_lost: bool = False
    maintenance: bool = False
    last_lock_check: float = 0.0
    failed_accounts: list[str] = field(default_factory=list)


@dataclass
class _Job:
    module_id: str
    name: str
    interval: float
    timeout: float
    fn: JobFn
    next_due: float | None = None
    run: _Run | None = None
    failure: str = ""

    @property
    def lock_key(self) -> str:
        return f"{_LOCK_PREFIX}:{self.module_id}:{self.name}"


class ModuleJobScheduler:
    """The ``schedule`` port of the control role and the thread that runs the jobs."""

    def __init__(
        self,
        *,
        services: Mapping[str, Any] | None = None,
        lease: AdvisoryJobLease | None = None,
        accounts: Callable[[], Iterable[str]] | None = None,
        maintenance_active: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        start_thread: Callable[[Callable[[], None], str], threading.Thread] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._services = dict(services or {})
        self._lease = lease
        self._accounts = accounts or (lambda: ())
        self._maintenance_active = maintenance_active
        self._clock = clock
        self._jitter = jitter
        self._start_thread = start_thread or _daemon_thread
        self._tick = max(0.05, float(tick_seconds))
        self._log = logger or _logger
        self._lock = threading.RLock()
        self._jobs: dict[tuple[str, str], _Job] = {}
        self._versions: dict[str, str] = {}
        self._requires: dict[str, tuple[str, ...]] = {}
        self._scope: str | None = None
        self._sealed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- registration (while the role loads its modules) -----------------------

    def __call__(
        self,
        name: str,
        interval_seconds: float,
        fn: JobFn,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Schedule ``fn(ctx)`` every ``interval_seconds`` for the module now loading."""
        with self._lock:
            if self._sealed:
                raise ModuleJobError("module jobs can be scheduled only while modules load")
            module_id = self._scope
            if not module_id:
                raise ModuleJobError(
                    "a module job needs the platform module identity Core loaded the module under"
                )
            job_name = str(name or "")
            if not _JOB_NAME_PATTERN.fullmatch(job_name):
                raise ModuleJobError(
                    f"module job name {job_name!r} must match {_JOB_NAME_PATTERN.pattern}"
                )
            interval = _seconds(interval_seconds, label=f"module job {job_name!r} interval")
            if interval < MIN_JOB_INTERVAL_SECONDS:
                raise ModuleJobError(
                    f"module job {job_name!r} interval must be at least "
                    f"{MIN_JOB_INTERVAL_SECONDS} seconds, got {interval:g}"
                )
            timeout = (
                interval
                if timeout_seconds is None
                else _seconds(timeout_seconds, label=f"module job {job_name!r} timeout")
            )
            if not callable(fn):
                raise ModuleJobError(f"module job {job_name!r} function is not callable")
            key = (module_id, job_name)
            if key in self._jobs:
                raise ModuleJobError(f"module {module_id!r} schedules job {job_name!r} twice")
            self._jobs[key] = _Job(
                module_id=module_id, name=job_name, interval=interval, timeout=timeout, fn=fn
            )

    @contextmanager
    def platform_module_scope(self, module_id: str | None, provides: Any = None) -> Iterator[None]:
        """Entered by the loader around one module's hook: jobs belong to that module."""
        del provides
        with self._lock:
            previous, self._scope = self._scope, module_id
        try:
            yield
        finally:
            with self._lock:
                self._scope = previous

    def loading_checkpoint(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._jobs)

    def restore_loading_checkpoint(self, checkpoint: tuple[tuple[str, str], ...] | None) -> None:
        """Drop every job scheduled after ``checkpoint`` (the failed module's jobs)."""
        keep = set(checkpoint or ())
        with self._lock:
            for key in [key for key in self._jobs if key not in keep]:
                del self._jobs[key]

    def seal(
        self,
        module_versions: Mapping[str, str] | None = None,
        module_requires: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """Loading finished: refuse further jobs; record the loaded modules' versions.

        ``module_requires`` maps a module id to the ids it declares under
        ``requires``; ports bound to a module identity are bound with them.
        """
        with self._lock:
            self._sealed = True
            self._versions.update({str(k): str(v) for k, v in (module_versions or {}).items()})
            self._requires.update(
                {str(k): tuple(str(i) for i in v) for k, v in (module_requires or {}).items()}
            )

    # --- introspection ---------------------------------------------------------

    def jobs(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(
                {
                    "module_id": job.module_id,
                    "name": job.name,
                    "interval_seconds": job.interval,
                    "timeout_seconds": job.timeout,
                }
                for job in self._jobs.values()
            )

    def failures(self) -> list[dict[str, str]]:
        """One record per module with a failing job, in the loader's record shape."""
        with self._lock:
            reasons: dict[str, list[str]] = {}
            for job in self._jobs.values():
                if job.failure:
                    reasons.setdefault(job.module_id, []).append(f"job {job.name}: {job.failure}")
            return [
                {
                    "id": module_id,
                    "version": self._versions.get(module_id, ""),
                    "status": MODULE_JOB_FAILED_STATUS,
                    "load_error": "; ".join(items),
                }
                for module_id, items in sorted(reasons.items())
            ]

    # --- running ---------------------------------------------------------------

    def start(self, stop_event: threading.Event | None = None) -> threading.Thread | None:
        """Start the scheduler thread; ``None`` when no module scheduled a job."""
        with self._lock:
            if not self._jobs or self._thread is not None:
                return self._thread
            if stop_event is not None:
                self._stop = stop_event
            self._thread = self._start_thread(self._loop, "control-platform-module-jobs")
            return self._thread

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def stop(self, *, wait: Callable[[threading.Thread, float], None] | None = None) -> None:
        """Stop starting jobs, ask running ones to stop, wait for them, then release locks.

        Each running job is waited for up to the rest of its timeout (it was asked
        to stop first, so a job that checks ``should_stop`` returns early). Only
        then is the lock session closed: closing it earlier would let another
        replica start a job this one still runs. A job still running after its
        wait is logged; the process is exiting and the thread goes with it.
        """
        join = wait or _join
        self._stop.set()
        scheduler = self._thread
        if (
            scheduler is not None
            and scheduler is not threading.current_thread()
            and scheduler.is_alive()
        ):
            join(scheduler, _SCHEDULER_JOIN_SECONDS)
        with self._lock:
            running = [(job, job.run) for job in self._jobs.values() if job.run is not None]
        for _job, run in running:
            run.stop.set()
        now = self._clock()
        for job, run in running:
            thread = run.thread
            if thread is threading.current_thread() or not thread.is_alive():
                continue
            join(thread, max(0.0, job.timeout - (now - run.started)))
            if thread.is_alive():
                self._log.warning(
                    "Platform module %s job %s still runs after its timeout while the "
                    "control role stops; its lock is released with the process",
                    job.module_id,
                    job.name,
                )
        if self._lease is not None:
            self._lease.close()

    def _loop(self) -> None:
        self._log.info("Platform module jobs started (%d job(s))", len(self._jobs))
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:
                self._log.exception("Platform module job scheduling pass failed")
            if self._stop.wait(self._tick):
                break
        self._log.info("Platform module jobs stopped")

    def run_pending(self) -> int:
        """One scheduling pass: flag overdue runs and start due jobs. Returns runs started."""
        if self._stop.is_set():
            return 0
        now = self._clock()
        paused = self._maintenance()
        started = 0
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            self._check_overdue(job, now)
            if paused:
                self._stop_for_maintenance(job)
            self._check_lock(job, now)
            if job.next_due is None:
                job.next_due = now + self._delay(job)
            if paused or job.run is not None or now < job.next_due:
                continue
            job.next_due = now + job.interval + self._delay(job)
            if self._stop.is_set():
                break
            if self._lease is not None and not self._lease.hold(job.lock_key):
                continue
            self._launch(job, now)
            started += 1
        return started

    def _maintenance(self) -> bool:
        """The maintenance signal; a signal that cannot be read counts as maintenance."""
        try:
            return bool(self._maintenance_active())
        except Exception:
            return True

    def _stop_for_maintenance(self, job: _Job) -> None:
        run = job.run
        if run is None or run.maintenance:
            return
        run.maintenance = True
        run.stop.set()
        self._log.warning(
            "Platform module %s job %s was asked to stop: maintenance started",
            job.module_id,
            job.name,
        )

    def _check_lock(self, job: _Job, now: float) -> None:
        """Every keepalive interval, confirm a running job's lock; stop the job without it."""
        run = job.run
        if run is None or run.lock_lost or self._lease is None:
            return
        if now - max(run.started, run.last_lock_check) < LOCK_KEEPALIVE_SECONDS:
            return
        run.last_lock_check = now
        if self._lease.hold(job.lock_key):
            return
        run.lock_lost = True
        run.stop.set()
        job.failure = "LockLost: the job lock was lost while the job ran"
        self._log.error(
            "Platform module %s %s job %s lost its lock while running; it was asked to "
            "stop and another replica may take the job over",
            job.module_id,
            self._versions.get(job.module_id, ""),
            job.name,
        )

    def _delay(self, job: _Job) -> float:
        bound = min(job.interval / 10.0, MAX_JOB_JITTER_SECONDS)
        try:
            fraction = min(max(float(self._jitter()), 0.0), 1.0)
        except Exception:
            fraction = 0.5
        return bound * fraction

    def _check_overdue(self, job: _Job, now: float) -> None:
        run = job.run
        if run is None or run.timed_out or now - run.started <= job.timeout:
            return
        run.timed_out = True
        run.stop.set()
        job.failure = f"TimeoutError: still running after {job.timeout:g} seconds"
        self._log.error(
            "Platform module %s %s job %s still runs after its %g second timeout; it was "
            "asked to stop and no new run starts until it returns",
            job.module_id,
            self._versions.get(job.module_id, ""),
            job.name,
            job.timeout,
        )

    def _launch(self, job: _Job, now: float) -> None:
        stop = threading.Event()
        run = _Run(thread=threading.current_thread(), started=now, stop=stop)
        job.run = run
        try:
            run.thread = self._start_thread(lambda: self._execute(job, run), _thread_name(job))
        except Exception as exc:
            job.run = None
            self._record_failure(job, exc)

    def _execute(self, job: _Job, run: _Run) -> None:
        context = self._job_context(job, run)
        try:
            job.fn(context)
        except Exception as exc:
            if not (run.timed_out or run.lock_lost):
                self._record_failure(job, exc)
            else:
                self._log.warning(
                    "Platform module %s job %s raised after its timeout: %s",
                    job.module_id,
                    job.name,
                    module_failure_reason(exc),
                )
        else:
            # A run that overran or lost its lock keeps that failure until a clean run.
            if run.timed_out or run.lock_lost:
                pass
            elif run.failed_accounts:
                job.failure = f"{len(run.failed_accounts)} account(s) failed: " + ", ".join(
                    run.failed_accounts[:5]
                )
            else:
                job.failure = ""
        finally:
            with self._lock:
                if job.run is run:
                    job.run = None

    def _record_failure(self, job: _Job, exc: BaseException) -> None:
        job.failure = module_failure_reason(exc)
        self._log.error(
            "Platform module %s %s job %s failed; it runs again at its next due time: %s",
            job.module_id,
            self._versions.get(job.module_id, ""),
            job.name,
            job.failure,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def _job_context(self, job: _Job, run: _Run) -> Mapping[str, Any]:
        services = bind_platform_module_services(
            self._services, job.module_id, self._requires.get(job.module_id, ())
        )

        def should_stop() -> bool:
            # Maintenance is read live: a run stops as soon as it starts, not only
            # at the scheduler's next pass.
            return run.stop.is_set() or self._stop.is_set() or self._maintenance()

        def entitled(account_id: str, feature: str) -> bool:
            decider = services.get("entitlement_service")
            if decider is None or not callable(getattr(decider, "is_entitled", None)):
                return False
            return decider.is_entitled(account_id, feature) is True

        def for_each_account(
            fn: Callable[[str], Any], *, feature: str | None = None
        ) -> dict[str, Any]:
            processed = 0
            skipped = 0
            failed: list[str] = []
            wanted = str(feature or "").strip()
            for account_id in list(self._accounts()):
                if should_stop():
                    break
                if wanted:
                    try:
                        allowed = entitled(account_id, wanted)
                    except Exception as exc:
                        failed.append(account_id)
                        self._log.error(
                            "Platform module %s job %s could not check %s for account %s: %s",
                            job.module_id,
                            job.name,
                            wanted,
                            account_id,
                            module_failure_reason(exc),
                        )
                        continue
                    if not allowed:
                        skipped += 1
                        continue
                processed += 1
                try:
                    fn(account_id)
                except Exception as exc:
                    failed.append(account_id)
                    self._log.error(
                        "Platform module %s job %s failed for account %s: %s",
                        job.module_id,
                        job.name,
                        account_id,
                        module_failure_reason(exc),
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            run.failed_accounts.extend(failed)
            if wanted:
                self._log.info(
                    "Platform module %s job %s: %d account(s) entitled to %s processed, "
                    "%d not entitled skipped",
                    job.module_id,
                    job.name,
                    processed,
                    wanted,
                    skipped,
                )
            return {"processed": processed, "failed": failed, "skipped": skipped}

        return MappingProxyType(
            {
                **services,
                "role": "control",
                "platform_module": MappingProxyType(
                    {"id": job.module_id, "version": self._versions.get(job.module_id, "")}
                ),
                "job": job.name,
                "should_stop": should_stop,
                "for_each_account": for_each_account,
            }
        )


def _seconds(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModuleJobError(f"{label} must be a number of seconds")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ModuleJobError(f"{label} must be a positive number of seconds")
    return seconds


def _join(thread: threading.Thread, seconds: float) -> None:
    thread.join(seconds)


def _thread_name(job: _Job) -> str:
    return f"platform-module-job:{job.module_id}:{job.name}"


def _daemon_thread(target: Callable[[], None], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


__all__ = [
    "LOCK_KEEPALIVE_SECONDS",
    "MIN_JOB_INTERVAL_SECONDS",
    "MODULE_JOB_FAILED_STATUS",
    "AdvisoryJobLease",
    "ModuleJobError",
    "ModuleJobScheduler",
    "active_account_lister",
    "postgres_connector",
]
