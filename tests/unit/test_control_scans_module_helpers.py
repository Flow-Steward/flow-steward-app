from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import core.infrastructure.runtime.control.scans as scans
from core.application.jobs import artifact_cleanup_recovery as artifact_cleanup_runtime_recovery
from core.application.orchestration.run_definition import clear_run_definition_cache
from core.infrastructure.runtime.control.config import ControlConfig
from tests._step_run_state import step_run_state


def _cfg(**overrides: Any) -> ControlConfig:
    base = {
        "watchdog_enabled": True,
        "watchdog_interval": 30.0,
        "watchdog_auto_apply": True,
        "watchdog_stuck_seconds": 120,
        "watchdog_orphan_grace_seconds": 120,
        "watchdog_pickup_grace_seconds": 45,
        "watchdog_max_journal_entries": 200,
        "scheduled_jobs_enabled": True,
        "scheduled_jobs_interval": 30.0,
        "scheduled_jobs_batch_limit": 20,
        "dashboard_rollup_enabled": True,
        "dashboard_rollup_interval": 300.0,
        "dashboard_rollup_backfill_days": 180,
        "dashboard_rollup_lookback_days": 3,
        "security_posture_enabled": True,
        "security_posture_interval": 600.0,
        "workflow_knowledge_sync_enabled": True,
        "workflow_knowledge_sync_interval": 120.0,
        "workflow_knowledge_sync_changelog_limit": 200,
    }
    base.update(overrides)
    return ControlConfig(**base)


def test_small_scalar_helpers() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    assert scans._as_utc_datetime(now) == now
    assert scans._as_utc_datetime("2026-03-29T12:00:00Z").tzinfo is UTC
    assert scans._as_utc_datetime("invalid", fallback=now) == now
    assert scans._as_utc_datetime("invalid", fallback=None) is None

    assert scans._to_json_obj({"a": 1}) == {"a": 1}
    assert scans._to_json_obj("[1,2,3]") == [1, 2, 3]
    assert scans._to_json_obj("broken-json") == "broken-json"
    assert scans._to_json_obj(None) is None

    assert scans._parse_worker_pid("worker-123") == 123
    assert scans._parse_worker_pid("worker-x") is None
    assert scans._parse_worker_pid(None) is None


def test_workflow_knowledge_scan_persists_success_and_failure(monkeypatch) -> None:
    from core.application.jobs import workflow_knowledge_sync_requests as requests

    class _StatusStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def record_workflow_knowledge_sync_loop_attempt(self):
            self.calls.append("attempt")

        def record_workflow_knowledge_sync_loop_success(self):
            self.calls.append("success")

        def record_workflow_knowledge_sync_loop_failure(self, reason):
            self.calls.append(f"failure:{reason}")

        def list_recent_registry_changelog(self, **_kwargs):
            return []

    monkeypatch.setattr(
        requests,
        "enqueue_workflow_knowledge_sync_batch",
        lambda **_kwargs: {"status": "queued", "scope_count": 1},
    )
    status = _StatusStore()
    events = object()

    assert scans.workflow_knowledge_sync_scan(status, events, _cfg()) == {
        "status": "queued",
        "scope_count": 1,
    }
    # AC17-R149: the attempt is the worker's to record, with its claim; a request is not one.
    assert status.calls == []

    def _fail(**_kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(requests, "enqueue_workflow_knowledge_sync_batch", _fail)
    try:
        scans.workflow_knowledge_sync_scan(status, events, _cfg())
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected workflow knowledge sync failure")
    assert status.calls == ["failure:outbox unavailable"]


def test_pid_alive_uses_proc_first(monkeypatch) -> None:
    monkeypatch.setattr(scans.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(scans.os.path, "exists", lambda path: path.endswith("/321"))
    assert scans._pid_alive(321) is True
    assert scans._pid_alive(322) is False
    assert scans._pid_alive(0) is False


def test_pid_alive_kill_fallback(monkeypatch) -> None:
    monkeypatch.setattr(scans.os.path, "isdir", lambda _path: False)

    class _Result:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr(
        scans.subprocess,
        "run",
        lambda *_args, **_kwargs: _Result(returncode=1, stdout=""),
    )
    assert scans._pid_alive(10) is False

    monkeypatch.setattr(
        scans.subprocess,
        "run",
        lambda *_args, **_kwargs: _Result(returncode=0, stdout=" 10\n"),
    )
    assert scans._pid_alive(10) is True


def test_lease_helpers() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    lease = {
        "heartbeat_at": now - timedelta(seconds=12),
        "acquired_at": now - timedelta(seconds=30),
        "expires_at": now + timedelta(seconds=10),
    }
    assert scans._lease_heartbeat_age_seconds(lease, now) == 12
    assert scans._lease_timestamp_age_seconds(lease, now, "acquired_at") == 30
    assert scans._lease_is_live(lease, now, orphan_grace=120) is True

    stale = {
        "heartbeat_at": now - timedelta(seconds=500),
        "expires_at": now - timedelta(seconds=1),
    }
    assert scans._lease_is_live(stale, now, orphan_grace=120) is False


def test_index_active_project_leases_filters_rows() -> None:
    class _BL:
        @staticmethod
        def list_project_execution_leases(active_only: bool = True) -> list[Any]:
            assert active_only is True
            return [
                {"project_id": 0},
                {"project_id": ""},
                {"project_id": 11, "active_job_id": 101},
                {"project_id": 11, "active_job_id": 102},  # duplicate ignored
                {"project_id": 12, "active_job_id": 201},
                "skip-me",
            ]

    out = scans._index_active_project_leases(_BL())
    assert [row["active_job_id"] for row in out["11"]] == [101, 102]
    assert [row["active_job_id"] for row in out["12"]] == [201]
    assert len(out) == 2


def test_classify_runtime_health_orphaned_and_stuck_paths(monkeypatch) -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    cfg = _cfg(watchdog_stuck_seconds=120, watchdog_pickup_grace_seconds=45)

    monkeypatch.setattr(scans, "_pid_alive", lambda _pid: False)
    orphan = scans._classify_runtime_health(
        {
            "id": 1,
            "project_id": 5,
            "status": "in_progress",
            "worker_id": "worker-123",
            "started_at": now - timedelta(seconds=600),
            "heartbeat_at": now - timedelta(seconds=240),
        },
        now,
        project_leases=None,
        cfg=cfg,
    )
    assert orphan is not None
    assert orphan["classification"] == "orphaned_worker_missing"
    assert orphan["target_status"] == "orphaned"

    # Missing heartbeat branch.
    monkeypatch.setattr(scans, "_pid_alive", lambda _pid: True)
    missing_hb = scans._classify_runtime_health(
        {
            "id": 2,
            "project_id": 5,
            "status": "in_progress",
            "worker_id": "worker-999",
            "started_at": now - timedelta(seconds=300),
            "heartbeat_at": None,
        },
        now,
        project_leases=None,
        cfg=cfg,
    )
    assert missing_hb is not None
    assert missing_hb["classification"] == "stuck_missing_heartbeat"
    assert missing_hb["target_status"] == "stuck"

    pending = scans._classify_runtime_health(
        {
            "id": 3,
            "project_id": 9,
            "status": "pending",
            "queued_at": now - timedelta(seconds=400),
        },
        now,
        project_leases=None,
        cfg=cfg,
    )
    assert pending is not None
    assert pending["classification"] == "stuck_pending_queue"


@pytest.mark.parametrize("status", ["awaiting_approval", "awaiting_timer", "awaiting_child"])
def test_classify_runtime_health_never_marks_a_durable_wait(monkeypatch, status) -> None:
    """A paused run holds no worker: a dead PID and an old heartbeat mean nothing."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(scans, "_pid_alive", lambda _pid: False)

    row = scans._classify_runtime_health(
        {
            "id": 4,
            "project_id": 5,
            "status": status,
            "worker_id": "worker-123",
            "started_at": now - timedelta(days=3),
            "queued_at": now - timedelta(days=3),
            "heartbeat_at": now - timedelta(days=3),
        },
        now,
        project_leases=None,
        cfg=_cfg(watchdog_stuck_seconds=120, watchdog_pickup_grace_seconds=45),
    )

    assert row is None


def test_classify_runtime_health_pickup_grace_short_circuits() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    cfg = _cfg(watchdog_pickup_grace_seconds=60)
    row = scans._classify_runtime_health(
        {
            "id": 10,
            "project_id": 1,
            "status": "in_progress",
            "started_at": now - timedelta(seconds=15),
            "heartbeat_at": now - timedelta(seconds=15),
        },
        now,
        project_leases=[{"active_job_id": 10, "heartbeat_at": now - timedelta(seconds=1)}],
        cfg=cfg,
    )
    assert row is None


def test_classify_runtime_health_pending_waits_when_project_at_capacity() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    cfg = _cfg(watchdog_stuck_seconds=120, watchdog_pickup_grace_seconds=45)

    row = scans._classify_runtime_health(
        {
            "id": 88,
            "project_id": 5,
            "status": "pending",
            "queued_at": now - timedelta(seconds=500),
        },
        now,
        project_leases=[
            {
                "active_job_id": 40,
                "heartbeat_at": now - timedelta(seconds=5),
                "expires_at": now + timedelta(seconds=30),
            },
            {
                "active_job_id": 41,
                "heartbeat_at": now - timedelta(seconds=5),
                "expires_at": now + timedelta(seconds=30),
            },
        ],
        project_concurrency_cap=2,
        cfg=cfg,
    )

    assert row is None


def test_classify_runtime_health_freezes_pending_age_during_maintenance() -> None:
    now = datetime(2026, 3, 29, 14, 0, tzinfo=UTC)
    maintenance_activated_at = now - timedelta(hours=1)
    cfg = _cfg(watchdog_stuck_seconds=120)

    paused = scans._classify_runtime_health(
        {
            "id": 90,
            "project_id": 5,
            "status": "pending",
            "queued_at": maintenance_activated_at - timedelta(seconds=6),
        },
        now,
        project_leases=None,
        maintenance_activated_at=maintenance_activated_at,
        cfg=cfg,
    )
    already_stale = scans._classify_runtime_health(
        {
            "id": 91,
            "project_id": 5,
            "status": "pending",
            "queued_at": maintenance_activated_at - timedelta(seconds=121),
        },
        now,
        project_leases=None,
        maintenance_activated_at=maintenance_activated_at,
        cfg=cfg,
    )

    assert paused is None
    assert already_stale is not None
    assert already_stale["classification"] == "stuck_pending_queue"


def test_classify_runtime_health_keeps_running_failures_during_maintenance(
    monkeypatch,
) -> None:
    now = datetime(2026, 3, 29, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(scans, "_pid_alive", lambda _pid: True)

    running = scans._classify_runtime_health(
        {
            "id": 92,
            "project_id": 5,
            "status": "in_progress",
            "worker_id": "worker-123",
            "started_at": now - timedelta(minutes=10),
            "heartbeat_at": now - timedelta(minutes=5),
        },
        now,
        project_leases=None,
        maintenance_activated_at=now - timedelta(hours=1),
        cfg=_cfg(watchdog_stuck_seconds=120),
    )

    assert running is not None
    assert running["classification"] == "stuck_heartbeat_timeout"


def test_classify_runtime_health_does_not_suppress_on_invalid_maintenance_time() -> None:
    now = datetime(2026, 3, 29, 14, 0, tzinfo=UTC)

    pending = scans._classify_runtime_health(
        {
            "id": 93,
            "project_id": 5,
            "status": "pending",
            "queued_at": now - timedelta(minutes=5),
        },
        now,
        project_leases=None,
        maintenance_activated_at=datetime(2026, 3, 29, 13, 0),
        cfg=_cfg(watchdog_stuck_seconds=120),
    )

    assert pending is not None
    assert pending["classification"] == "stuck_pending_queue"


def test_classify_runtime_health_pending_waits_when_project_has_live_work() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    cfg = _cfg(watchdog_stuck_seconds=120, watchdog_pickup_grace_seconds=45)

    row = scans._classify_runtime_health(
        {
            "id": 89,
            "project_id": 5,
            "status": "pending",
            "queued_at": now - timedelta(seconds=500),
        },
        now,
        project_leases=[
            {
                "active_job_id": 40,
                "heartbeat_at": now - timedelta(seconds=5),
                "expires_at": now + timedelta(seconds=30),
            }
        ],
        project_concurrency_cap=4,
        cfg=cfg,
    )

    assert row is None


def test_classify_watchdog_recovery() -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    cfg = _cfg()
    recovery = scans._classify_watchdog_recovery(
        {
            "id": 77,
            "status": "stuck",
            "heartbeat_at": now - timedelta(seconds=10),
        },
        now,
        project_leases=[{"active_job_id": 77, "heartbeat_at": now - timedelta(seconds=5)}],
        cfg=cfg,
    )
    assert recovery is not None
    assert recovery["target_status"] == "in_progress"
    assert "live_project_lease" in recovery["reasons"]

    assert (
        scans._classify_watchdog_recovery(
            {"id": 77, "status": "completed"},
            now,
            project_leases=None,
            cfg=cfg,
        )
        is None
    )

    assert (
        scans._classify_watchdog_recovery(
            {
                "id": 77,
                "status": "stuck",
                "finished_at": now - timedelta(seconds=1),
                "heartbeat_at": now - timedelta(seconds=10),
            },
            now,
            project_leases=[{"active_job_id": 77, "heartbeat_at": now - timedelta(seconds=5)}],
            cfg=cfg,
        )
        is None
    )


def test_snapshot_and_step_hint_helpers() -> None:
    assert scans._sanitize_retryable_interrupted_step_snapshot(None) is None
    assert (
        scans._sanitize_retryable_interrupted_step_snapshot({"agent_id": "a", "skill": ""}) is None
    )
    cleaned = scans._sanitize_retryable_interrupted_step_snapshot(
        {
            "agent_id": "agent-x",
            "skill": "cap-y",
            "step_id": "step-z",
            "system_prompt": "s" * 15000,
            "user_prompt": "u" * 30000,
        }
    )
    assert cleaned is not None
    assert len(cleaned["system_prompt"]) == 12000
    assert len(cleaned["user_prompt"]) == 24000

    state = step_run_state({"step_id": "s1", "agent_id": "dev", "skill": "implement"})
    hint = scans._extract_runtime_step_hint(state)
    assert hint == {
        "step_id": "s1",
        "step_execution_id": state["run_cursor"]["step_execution_id"],
        "agent_id": "dev",
        "skill": "implement",
    }


def test_step_hint_keeps_the_cursor_step_when_the_pinned_definition_is_unavailable() -> None:
    state = step_run_state({"step_id": "s1", "agent_id": "dev", "skill": "implement"})
    clear_run_definition_cache()

    # No job row to load the archive from: the cursor still names the step.
    hint = scans._extract_runtime_step_hint(state)

    assert hint == {"step_id": "s1", "step_execution_id": state["run_cursor"]["step_execution_id"]}
    assert scans._extract_runtime_step_hint({}) == {}


def test_detect_retryable_interrupted_step_snapshot_prompt_path(monkeypatch) -> None:
    now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        scans,
        "get_thread_state_values",
        lambda _thread_id: step_run_state(
            {"step_id": "design", "agent_id": "planner", "skill": "design"}
        ),
    )

    class _BL:
        @staticmethod
        def get_logs(_job_id: int, since_id: int = 0, limit: int = 4000) -> list[dict[str, Any]]:
            assert since_id == 0
            assert limit == 4000
            return [
                {
                    "id": 10,
                    "action_type": "prompt_request",
                    "agent": "planner",
                    "meta": {"correlation_id": "corr-1", "step_id": "design", "skill": "design"},
                    "payload": {
                        "system_prompt": "sys",
                        "user_prompt": "usr",
                        "prompt_version": "v1",
                    },
                    "model_used": "gpt-5.4-mini",
                }
            ]

        @staticmethod
        def get_latest_agent_message(_job_id: int) -> dict[str, Any]:
            return {"id": 99, "correlation_id": "corr-1", "skill": "design"}

    snapshot = scans._detect_retryable_interrupted_step_snapshot(
        _BL(),
        {"id": 500, "thread_id": "thread-1", "current_agent": "planner", "heartbeat_at": now},
    )
    assert snapshot is not None
    assert snapshot["detection_source"] == "prompt_request_without_response"
    assert snapshot["agent_id"] == "planner"
    assert snapshot["skill"] == "design"
    assert snapshot["step_id"] == "design"
    assert snapshot["step_execution_id"]
    assert "phase_id" not in snapshot


def test_wrapper_functions_delegate(monkeypatch) -> None:
    cfg = _cfg()

    monkeypatch.setattr(
        scans,
        "_scheduled_enqueue_task_standalone",
        lambda *_args, **_kwargs: 123,
    )
    assert (
        scans._enqueue_task_standalone(
            SimpleNamespace(),
            task_id="t1",
            task_summary="sum",
            task_source="manual",
            project_id=1,
            project_name="p",
            project_config={},
            workflow_id="wf",
            test_mode=False,
            extra_state={},
            meta={},
            pre_publish_fn=None,
        )
        == 123
    )

    monkeypatch.setattr(
        scans,
        "_scheduled_run_scheduled_job_once",
        lambda *_args, **_kwargs: {"ok": True},
    )
    assert scans._run_scheduled_job_once(SimpleNamespace(), {"id": 1}, cfg)["ok"] is True

    monkeypatch.setattr(
        scans,
        "_scheduled_jobs_scan_impl",
        lambda *_args, **_kwargs: {"scan": "ok"},
    )
    assert scans.scheduled_jobs_scan(SimpleNamespace(), cfg)["scan"] == "ok"

    monkeypatch.setattr(scans, "_dashboard_rollup_scan_impl", lambda *_args, **_kwargs: {"d": 1})
    assert scans.dashboard_rollup_scan(SimpleNamespace(), cfg) == {"d": 1}

    monkeypatch.setattr(scans, "_security_posture_scan_impl", lambda *_args, **_kwargs: {"s": 1})
    assert scans.security_posture_scan(SimpleNamespace(), cfg) == {"s": 1}


def test_dispatch_outbox_scan_recovers_expired_preparing_submissions(monkeypatch) -> None:
    from core.application.jobs import task_enqueue_runtime

    storage = SimpleNamespace()
    recover_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        artifact_cleanup_runtime_recovery,
        "resolve_artifact_storage_adapter_from_env",
        lambda: storage,
    )
    monkeypatch.setattr(
        artifact_cleanup_runtime_recovery,
        "recover_expired_preparing_submissions",
        lambda bl, **kwargs: (
            recover_calls.append({"bl": bl, **kwargs})
            or {"claimed": 1, "recovered": 1, "cleanup_failed": 0, "superseded": 0}
        ),
    )
    monkeypatch.setattr(
        task_enqueue_runtime,
        "relay_pending_dispatch_outbox",
        lambda **_kwargs: {"claimed": 0, "published": 0, "superseded": 0, "failed": 0},
    )
    bl = SimpleNamespace()

    result = scans.dispatch_outbox_scan(bl)

    assert result["preparing_recovery"]["recovered"] == 1
    assert recover_calls == [{"bl": bl, "storage": storage}]


def test_dispatch_outbox_scan_relays_before_reconciliation_and_survives_its_failure(
    monkeypatch,
) -> None:
    from core.application.jobs import task_enqueue_runtime

    calls: list[str] = []
    storage = SimpleNamespace()
    monkeypatch.setattr(
        artifact_cleanup_runtime_recovery,
        "resolve_artifact_storage_adapter_from_env",
        lambda: storage,
    )
    monkeypatch.setattr(
        artifact_cleanup_runtime_recovery,
        "recover_expired_preparing_submissions",
        lambda *_args, **_kwargs: {
            "claimed": 0,
            "recovered": 0,
            "cleanup_failed": 0,
            "superseded": 0,
        },
    )
    monkeypatch.setattr(
        task_enqueue_runtime,
        "relay_pending_dispatch_outbox",
        lambda **_kwargs: (
            calls.append("relay") or {"claimed": 1, "published": 1, "superseded": 0, "failed": 0}
        ),
    )
    monkeypatch.setattr(
        artifact_cleanup_runtime_recovery,
        "reconcile_job_artifact_purge_markers",
        lambda *_args, **_kwargs: (
            calls.append("reconcile")
            or (_ for _ in ()).throw(RuntimeError("checkpoint unavailable"))
        ),
    )

    result = scans.dispatch_outbox_scan(SimpleNamespace())

    assert calls == ["relay", "reconcile"]
    assert result["published"] == 1
    assert result["artifact_cleanup_reconciliation"]["failed"] == 1


class TestSignalableWorkerPidRS5035:
    """A worker pid may only be signalled from the namespace that recorded it.

    A worker id ends in its pid and a containerised worker is normally pid 1 of
    its own container. Signalling that number from the API container hits the
    API's own init: the documented stuck-job recovery action took the platform
    down instead of recovering the job.
    """

    def test_pid_one_of_another_container_is_never_signalled(self, monkeypatch) -> None:
        """This is the outage: the API's own init is pid 1 too."""
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        assert scans._parse_worker_pid("af41b2f16bc6-1") == 1, "the pid is still parseable"
        assert scans.signalable_worker_pid("af41b2f16bc6-1") is None

    def test_pid_on_another_host_is_not_signalled(self, monkeypatch) -> None:
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        assert scans.signalable_worker_pid("af41b2f16bc6-4242") is None

    def test_pid_one_on_this_host_is_not_signalled(self, monkeypatch) -> None:
        """Even locally, pid 1 is this process's own init rather than a worker."""
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        assert scans.signalable_worker_pid("fs-app-host-1") is None

    def test_local_worker_pid_is_signalled(self, monkeypatch) -> None:
        """The case the kill action exists for still works."""
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        assert scans.signalable_worker_pid("fs-app-host-4242") == 4242

    def test_local_incarnation_aware_worker_pid_is_signalled(self, monkeypatch) -> None:
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        worker_id = "fs-app-host-a1b2c3d4e5f6-4242"

        assert scans._parse_worker_pid(worker_id) == 4242
        assert scans.signalable_worker_pid(worker_id) == 4242

    def test_hostnames_with_dashes_are_parsed_from_the_right_end(self, monkeypatch) -> None:
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-worker-node-7")
        assert scans.signalable_worker_pid("fs-worker-node-7-991") == 991
        assert scans.signalable_worker_pid("fs-worker-node-8-991") is None

    def test_malformed_worker_ids_are_not_signalled(self, monkeypatch) -> None:
        monkeypatch.setattr(scans.socket, "gethostname", lambda: "fs-app-host")
        for worker_id in ("", None, "no-pid-here", "-5", "fs-app-host-"):
            assert scans.signalable_worker_pid(worker_id) is None, worker_id
