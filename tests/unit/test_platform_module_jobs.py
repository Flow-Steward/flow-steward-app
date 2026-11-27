"""Periodic jobs platform modules run in the control role (ADR 0082, 2026-11-27)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from core.infrastructure.platform_modules.loader import ModuleRegistrationRollback
from core.infrastructure.platform_modules.module_jobs import (
    MIN_JOB_INTERVAL_SECONDS,
    AdvisoryJobLease,
    ModuleJobError,
    ModuleJobScheduler,
    active_account_lister,
)

MODULE = "acme.sync"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _sync_thread(target, _name):
    target()
    return threading.current_thread()


class Lease:
    def __init__(self, held: bool = True) -> None:
        self.held = held
        self.keys: list[str] = []
        self.closed = False

    def hold(self, key: str) -> bool:
        self.keys.append(key)
        return self.held

    def close(self) -> None:
        self.closed = True


def _scheduler(**kwargs) -> tuple[ModuleJobScheduler, Clock]:
    clock = Clock()
    defaults = {
        "clock": clock,
        "jitter": lambda: 0.0,
        "start_thread": _sync_thread,
    }
    defaults.update(kwargs)
    return ModuleJobScheduler(**defaults), clock


def _schedule(scheduler, name="sync", interval=60, fn=None, module_id=MODULE, **kwargs):
    with scheduler.platform_module_scope(module_id, frozenset()):
        scheduler(name, interval, fn or (lambda _ctx: None), **kwargs)


# --- registration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "interval", "kwargs"),
    [
        ("sync", MIN_JOB_INTERVAL_SECONDS - 1, {}),
        ("sync", 0, {}),
        ("sync", True, {}),
        ("sync", "60", {}),
        ("sync", float("inf"), {}),
        ("Sync", 60, {}),
        ("", 60, {}),
        ("sync", 60, {"timeout_seconds": 0}),
    ],
)
def test_a_job_core_cannot_run_is_refused(name, interval, kwargs) -> None:
    scheduler, _clock = _scheduler()

    with pytest.raises(ModuleJobError):
        _schedule(scheduler, name=name, interval=interval, **kwargs)

    assert scheduler.jobs() == ()


def test_a_job_belongs_to_the_module_loading_and_is_refused_outside_loading() -> None:
    scheduler, _clock = _scheduler()

    with pytest.raises(ModuleJobError, match="identity"):
        scheduler("sync", 60, lambda _ctx: None)
    with pytest.raises(ModuleJobError, match="identity"):
        _schedule(scheduler, module_id=None)
    _schedule(scheduler)
    with pytest.raises(ModuleJobError, match="twice"):
        _schedule(scheduler)
    # The same name in another module is another job.
    _schedule(scheduler, module_id="acme.other")
    scheduler.seal({MODULE: "1.0.0"})
    with pytest.raises(ModuleJobError, match="only while modules load"):
        _schedule(scheduler, name="later")

    assert [(job["module_id"], job["name"]) for job in scheduler.jobs()] == [
        (MODULE, "sync"),
        ("acme.other", "sync"),
    ]
    assert scheduler.jobs()[0]["timeout_seconds"] == 60


def test_a_failed_module_loses_the_jobs_it_scheduled() -> None:
    scheduler, _clock = _scheduler()
    _schedule(scheduler, module_id="acme.first")
    rollback = ModuleRegistrationRollback.capture(services={"schedule": scheduler})

    with rollback.loading_scope(SimpleNamespace(module_id=MODULE, provides=frozenset())):
        scheduler("sync", 60, lambda _ctx: None)
    rollback.restore()

    assert [job["module_id"] for job in scheduler.jobs()] == ["acme.first"]


def test_the_loader_hands_the_schedule_port_to_the_hook_under_the_module_identity(
    tmp_path, monkeypatch
) -> None:
    from core.infrastructure.platform_modules import loader

    scheduler, _clock = _scheduler()
    module = loader.PreparedPlatformModule(
        module_id=MODULE, version="2.0.0", import_name="fs_sync", path=tmp_path
    )
    hook_contexts: list = []

    def register_control(context) -> None:
        hook_contexts.append(context)
        context["schedule"]("sync", 120, lambda _ctx: None, timeout_seconds=30)

    monkeypatch.setattr(
        loader, "activate_prepared_platform_modules", lambda *_args, **_kwargs: (module,)
    )
    monkeypatch.setattr(
        loader,
        "import_prepared_platform_module",
        lambda *_args, **_kwargs: SimpleNamespace(register_control=register_control),
    )

    loaded = loader.register_platform_modules_for_role(
        "control", config={}, services={"schedule": scheduler}
    )

    assert [item.module_id for item in loaded] == [MODULE]
    assert hook_contexts[0]["schedule"] is scheduler
    assert scheduler.jobs() == (
        {"module_id": MODULE, "name": "sync", "interval_seconds": 120.0, "timeout_seconds": 30.0},
    )


# --- running --------------------------------------------------------------------


def test_a_job_runs_after_its_jitter_then_once_per_interval() -> None:
    runs: list[float] = []
    scheduler, clock = _scheduler(jitter=lambda: 1.0)
    _schedule(scheduler, interval=100, fn=lambda _ctx: runs.append(clock.now))
    scheduler.seal({MODULE: "1.0.0"})

    assert scheduler.run_pending() == 0  # due after up to a tenth of the interval
    clock.now += 10
    assert scheduler.run_pending() == 1
    clock.now += 100
    assert scheduler.run_pending() == 0
    clock.now += 10
    assert scheduler.run_pending() == 1
    assert runs == [1010.0, 1120.0]


def test_the_jitter_is_at_most_a_minute_even_for_long_intervals() -> None:
    scheduler, clock = _scheduler(jitter=lambda: 1.0)
    _schedule(scheduler, interval=86400)
    scheduler.run_pending()

    clock.now += 60
    assert scheduler.run_pending() == 1


def test_only_the_replica_holding_the_job_lock_runs_it() -> None:
    runs: list[str] = []
    lease = Lease(held=False)
    scheduler, _clock = _scheduler(lease=lease)
    _schedule(scheduler, fn=lambda ctx: runs.append(ctx["job"]))

    assert scheduler.run_pending() == 0
    lease.held = True
    scheduler._jobs[(MODULE, "sync")].next_due = 0
    assert scheduler.run_pending() == 1
    assert runs == ["sync"]
    assert lease.keys == [f"flow-steward:platform-module-job:{MODULE}:sync"] * 2
    scheduler.stop()
    assert lease.closed is True


def test_no_run_starts_during_maintenance_and_the_due_job_waits_for_it() -> None:
    paused = {"value": True}
    runs: list[int] = []
    scheduler, _clock = _scheduler(maintenance_active=lambda: paused["value"])
    _schedule(scheduler, fn=lambda _ctx: runs.append(1))

    assert scheduler.run_pending() == 0
    paused["value"] = False
    assert scheduler.run_pending() == 1
    assert runs == [1]


def test_the_job_context_carries_the_ports_and_the_module_identity() -> None:
    seen: list = []
    scheduler, _clock = _scheduler(services={"db": "db-factory"})
    _schedule(scheduler, fn=seen.append)
    scheduler.seal({MODULE: "1.4.0"})

    scheduler.run_pending()

    [context] = seen
    assert context["db"] == "db-factory"
    assert context["role"] == "control"
    assert dict(context["platform_module"]) == {"id": MODULE, "version": "1.4.0"}
    assert context["job"] == "sync"
    assert context["should_stop"]() is False
    with pytest.raises(TypeError):
        context["db"] = "other"


def test_a_failing_job_is_logged_listed_and_cleared_by_the_next_clean_run(caplog) -> None:
    outcome = {"fail": True}

    def job(_ctx) -> None:
        if outcome["fail"]:
            raise RuntimeError("upstream unavailable at postgresql://flow:secret@db/flow")

    scheduler, _clock = _scheduler()
    _schedule(scheduler, fn=job)
    scheduler.seal({MODULE: "1.0.0"})

    scheduler.run_pending()

    [record] = scheduler.failures()
    assert record["id"] == MODULE
    assert record["version"] == "1.0.0"
    assert record["status"] == "job_failed"
    assert record["load_error"].startswith("job sync: RuntimeError: upstream unavailable")
    assert "secret" not in record["load_error"]
    assert "job sync failed" in caplog.text

    outcome["fail"] = False
    scheduler._jobs[(MODULE, "sync")].next_due = 0
    scheduler.run_pending()
    assert scheduler.failures() == []


def test_one_account_failing_does_not_stop_the_others_and_is_listed() -> None:
    visited: list[str] = []
    summaries: list[dict] = []

    def per_account(account_id: str) -> None:
        visited.append(account_id)
        if account_id == "b":
            raise ValueError("bad data")

    scheduler, _clock = _scheduler(accounts=lambda: ["a", "b", "c"])
    _schedule(scheduler, fn=lambda ctx: summaries.append(ctx["for_each_account"](per_account)))

    scheduler.run_pending()

    assert visited == ["a", "b", "c"]
    assert summaries == [{"processed": 3, "failed": ["b"], "skipped": 0}]
    assert scheduler.failures()[0]["load_error"] == "job sync: 1 account(s) failed: b"


def test_an_overrunning_job_is_flagged_asked_to_stop_and_never_doubled() -> None:
    release = threading.Event()
    stopped = threading.Event()
    starts: list[int] = []

    def slow(ctx) -> None:
        starts.append(1)
        while not ctx["should_stop"]():
            if release.wait(0.01):
                break
        stopped.set()

    clock = Clock()
    scheduler = ModuleJobScheduler(clock=clock, jitter=lambda: 0.0)
    _schedule(scheduler, interval=60, fn=slow, timeout_seconds=30)
    scheduler.seal({MODULE: "1.0.0"})

    assert scheduler.run_pending() == 1
    clock.now += 31
    assert scheduler.run_pending() == 0
    assert stopped.wait(2)
    assert "TimeoutError" in scheduler.failures()[0]["load_error"]
    # The timed-out run returned; the failure stays until a clean run.
    clock.now += 60
    for _ in range(100):
        if scheduler._jobs[(MODULE, "sync")].run is None:
            break
        threading.Event().wait(0.01)
    assert scheduler.failures()
    assert len(starts) == 1


def test_a_still_running_job_is_not_started_again() -> None:
    release = threading.Event()
    starts: list[int] = []

    def slow(_ctx) -> None:
        starts.append(1)
        release.wait(2)

    clock = Clock()
    scheduler = ModuleJobScheduler(clock=clock, jitter=lambda: 0.0)
    _schedule(scheduler, interval=60, fn=slow, timeout_seconds=600)

    assert scheduler.run_pending() == 1
    clock.now += 61
    assert scheduler.run_pending() == 0
    release.set()
    assert starts == [1]
    scheduler.stop()


def test_without_jobs_no_thread_starts() -> None:
    scheduler, _clock = _scheduler()

    assert scheduler.start() is None


# --- the cross-replica lock -------------------------------------------------------


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None) -> None:
        if self.connection.broken:
            raise OSError("connection lost")
        self.connection.statements.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self.row = (self.connection.grant,)
        elif "pg_locks" in sql:
            self.row = (self.connection.still_held,)
        elif "FROM accounts" in sql:
            self.row = None
        else:
            self.row = (1,)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [("acct-1",), ("acct-2",)]


class FakeConnection:
    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.still_held = True
        self.broken = False
        self.closed = False
        self.statements: list = []

    def cursor(self):
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def test_the_lease_keeps_a_lock_it_took_and_retakes_it_after_the_session_dropped() -> None:
    connections: list[FakeConnection] = []

    def connect() -> FakeConnection:
        connections.append(FakeConnection())
        return connections[-1]

    lease = AdvisoryJobLease(connect)

    assert lease.hold("job-a") is True
    assert lease.hold("job-a") is True  # kept: confirmed in pg_locks, not taken again
    statements = [sql for sql, _ in connections[0].statements]
    assert statements[0] == "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))"
    assert "pg_locks" in statements[1] and "pg_backend_pid()" in statements[1]
    connections[0].broken = True
    assert lease.hold("job-a") is False  # the session and its locks are gone
    assert connections[0].closed is True
    assert lease.hold("job-a") is True
    assert len(connections) == 2
    lease.close()
    assert connections[1].closed is True


def test_the_lease_reports_a_lock_another_replica_holds() -> None:
    lease = AdvisoryJobLease(lambda: FakeConnection(grant=False))

    assert lease.hold("job-a") is False


def test_active_accounts_are_listed_on_a_short_lived_session() -> None:
    connection = FakeConnection()

    assert active_account_lister(lambda: connection)() == ["acct-1", "acct-2"]
    assert connection.closed is True
    assert active_account_lister(None)() == []


def test_a_session_that_silently_lost_its_lock_is_dropped_and_retaken() -> None:
    connections: list[FakeConnection] = []

    def connect() -> FakeConnection:
        connections.append(FakeConnection())
        return connections[-1]

    lease = AdvisoryJobLease(connect)
    assert lease.hold("job-a") is True
    # The session still answers, but its backend no longer holds the lock.
    connections[0].still_held = False

    assert lease.hold("job-a") is False
    assert connections[0].closed is True
    assert lease.hold("job-a") is True
    assert len(connections) == 2


def test_a_job_is_not_started_without_its_lock_and_stops_when_it_loses_it() -> None:
    from core.infrastructure.platform_modules.module_jobs import LOCK_KEEPALIVE_SECONDS

    release = threading.Event()
    stopped = threading.Event()

    def slow(ctx) -> None:
        while not ctx["should_stop"]():
            if release.wait(0.01):
                break
        stopped.set()

    lease = Lease(held=True)
    clock = Clock()
    scheduler = ModuleJobScheduler(clock=clock, jitter=lambda: 0.0, lease=lease)
    _schedule(scheduler, interval=600, fn=slow)
    scheduler.seal({MODULE: "1.0.0"})

    assert scheduler.run_pending() == 1
    clock.now += LOCK_KEEPALIVE_SECONDS - 1
    scheduler.run_pending()
    assert not stopped.is_set()  # no check before the keepalive interval
    lease.held = False
    clock.now += 2
    scheduler.run_pending()

    assert stopped.wait(2)
    assert "LockLost" in scheduler.failures()[0]["load_error"]
    assert lease.keys == [f"flow-steward:platform-module-job:{MODULE}:sync"] * 2
    release.set()


def test_a_running_job_is_asked_to_stop_when_maintenance_starts() -> None:
    maintenance = {"on": False}
    release = threading.Event()
    stop_seen = threading.Event()

    def slow(ctx) -> None:
        while not release.wait(0.01):
            if ctx["should_stop"]():
                stop_seen.set()
                return

    clock = Clock()
    scheduler = ModuleJobScheduler(
        clock=clock, jitter=lambda: 0.0, maintenance_active=lambda: maintenance["on"]
    )
    _schedule(scheduler, interval=600, fn=slow)

    assert scheduler.run_pending() == 1
    assert not stop_seen.wait(0.05)
    maintenance["on"] = True  # read live by should_stop, before any scheduler pass
    assert stop_seen.wait(2)
    release.set()


def test_an_unreadable_maintenance_signal_starts_no_job() -> None:
    seen: list = []

    def broken() -> bool:
        raise OSError("marker unreadable")

    scheduler, _clock = _scheduler(maintenance_active=broken)
    _schedule(scheduler, fn=seen.append)

    assert scheduler.run_pending() == 0  # fail closed: no start
    assert seen == []


def test_stop_waits_for_running_jobs_before_releasing_the_lock() -> None:
    order: list[str] = []
    started = threading.Event()

    def job(ctx) -> None:
        started.set()
        while not ctx["should_stop"]():
            threading.Event().wait(0.01)
        threading.Event().wait(0.05)
        order.append("job returned")

    class OrderedLease(Lease):
        def close(self) -> None:
            order.append("lock released")
            super().close()

    lease = OrderedLease(held=True)
    clock = Clock()
    scheduler = ModuleJobScheduler(clock=clock, jitter=lambda: 0.0, lease=lease)
    _schedule(scheduler, interval=600, fn=job, timeout_seconds=30)
    assert scheduler.run_pending() == 1
    assert started.wait(2)

    scheduler.stop()

    assert order == ["job returned", "lock released"]
    # Stopped: no new run starts.
    clock.now += 10_000
    assert scheduler.run_pending() == 0


def test_stop_waits_at_most_the_rest_of_the_timeout() -> None:
    waits: list[float] = []
    release = threading.Event()

    def stubborn(_ctx) -> None:
        release.wait(5)

    lease = Lease(held=True)
    clock = Clock()
    scheduler = ModuleJobScheduler(clock=clock, jitter=lambda: 0.0, lease=lease)
    _schedule(scheduler, interval=600, fn=stubborn, timeout_seconds=100)
    assert scheduler.run_pending() == 1
    clock.now += 40

    scheduler.stop(wait=lambda _thread, seconds: waits.append(seconds))

    assert waits == [60.0]
    assert lease.closed is True
    release.set()


class Entitlements:
    def __init__(self, entitled: set[str], broken: set[str] = frozenset()) -> None:
        self.entitled = entitled
        self.broken = broken
        self.asked: list[tuple[str, str]] = []

    def is_entitled(self, account_id: str, feature: str) -> bool:
        self.asked.append((account_id, feature))
        if account_id in self.broken:
            raise RuntimeError("entitlement source unavailable")
        return account_id in self.entitled


def test_for_each_account_can_keep_to_the_accounts_entitled_to_a_feature(caplog) -> None:
    visited: list[str] = []
    summaries: list[dict] = []
    decider = Entitlements({"a", "c"}, broken={"d"})
    scheduler, _clock = _scheduler(
        services={"entitlement_service": decider}, accounts=lambda: ["a", "b", "c", "d"]
    )
    _schedule(
        scheduler,
        fn=lambda ctx: summaries.append(ctx["for_each_account"](visited.append, feature="login")),
    )
    caplog.set_level("INFO")

    scheduler.run_pending()

    assert visited == ["a", "c"]
    assert summaries == [{"processed": 2, "failed": ["d"], "skipped": 1}]
    assert decider.asked == [("a", "login"), ("b", "login"), ("c", "login"), ("d", "login")]
    assert "1 not entitled skipped" in caplog.text


def test_without_an_entitlement_service_no_account_counts_as_entitled() -> None:
    visited: list[str] = []
    scheduler, _clock = _scheduler(accounts=lambda: ["a"])
    _schedule(scheduler, fn=lambda ctx: ctx["for_each_account"](visited.append, feature="login"))

    scheduler.run_pending()

    assert visited == []
