"""Durable job-admission quiescence for environment transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import CancellationToken
from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.lifecycle.job_quiescence_gateway import (
    LifecycleJobQuiescenceGateway,
    PostgresActiveJobsReader,
)


def test_lifecycle_reader_waits_only_for_executing_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db import job_activity_store, psycopg_runtime

    rows = [
        {"id": "queued", "status": "pending"},
        {"id": "running", "status": "in_progress"},
        {"id": "resuming", "status": "resume_pending"},
        {"id": "approval", "status": "awaiting_approval"},
    ]

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(psycopg_runtime, "connect", lambda _dsn: connection)
    monkeypatch.setattr(job_activity_store, "get_active_jobs", lambda *, conn: rows)

    active = PostgresActiveJobsReader("postgresql://fixture")()

    assert [row["id"] for row in active] == ["running"]
    assert connection.closed is True


def test_quiescence_marker_blocks_admission_and_resume_removes_it(tmp_path: Path) -> None:
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=list,
        sleep=lambda _seconds: None,
    )

    ownership = gateway.stop_accepting("environment-op-1")

    marker = tmp_path / ".flow-steward/runtime/job-admission-paused"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "owner_operation_id": "environment-op-1",
    }
    assert marker.stat().st_mode & 0o777 == 0o600
    assert ownership.owner_operation_id == "environment-op-1"
    assert ownership.created is True

    assert gateway.resume_accepting("different-operation") is False
    assert marker.exists()
    assert gateway.resume_accepting("environment-op-1") is True

    assert not marker.exists()


def test_environment_pause_resume_never_clears_manual_maintenance(tmp_path: Path) -> None:
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=list,
        sleep=lambda _seconds: None,
    )
    maintenance_marker = tmp_path / ".flow-steward/runtime/maintenance-state.json"
    maintenance_marker.parent.mkdir(parents=True)
    maintenance_marker.write_text('{"owner_id":"manual-maintenance"}\n', encoding="utf-8")

    gateway.stop_accepting("environment-op-1")
    assert gateway.resume_accepting("environment-op-1") is True

    assert maintenance_marker.read_text(encoding="utf-8") == ('{"owner_id":"manual-maintenance"}\n')


def test_retry_reconciles_only_stale_marker_and_retains_matching_unfinished_owner(
    tmp_path: Path,
) -> None:
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=list,
        sleep=lambda _seconds: None,
    )
    marker = tmp_path / ".flow-steward/runtime/job-admission-paused"

    gateway.stop_accepting("environment-op-1")
    state = gateway.reconcile_pause(("environment-op-1",))
    assert state is not None
    assert state.owner_operation_id == "environment-op-1"
    assert marker.exists()

    assert gateway.reconcile_pause(()) is None
    assert not marker.exists()


def test_ambiguous_unfinished_operations_fail_safe_without_clearing_marker(
    tmp_path: Path,
) -> None:
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=list,
        sleep=lambda _seconds: None,
    )
    marker = tmp_path / ".flow-steward/runtime/job-admission-paused"
    gateway.stop_accepting("environment-op-1")

    with pytest.raises(LifecycleError, match="ambiguous"):
        gateway.reconcile_pause(("environment-op-1", "environment-op-2"))

    assert marker.exists()


def test_wait_for_idle_polls_without_terminating_jobs(tmp_path: Path) -> None:
    active = [[{"id": "job-1"}], [], []]
    sleeps: list[float] = []
    heartbeats: list[str] = []
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=lambda: active.pop(0),
        sleep=sleeps.append,
    )

    gateway.wait_for_idle(
        cancellation=CancellationToken(),
        heartbeat=heartbeats.append,
    )

    assert sleeps == [1.0, 1.0]
    assert heartbeats == [
        "Waiting for 1 active job to finish safely.",
        "Confirming job admission remains idle.",
    ]


def test_wait_for_idle_cancels_transition_without_killing_jobs(tmp_path: Path) -> None:
    cancellation = CancellationToken()
    cancellation.request()
    gateway = LifecycleJobQuiescenceGateway(
        project_root=tmp_path,
        active_jobs=lambda: [{"id": "job-1"}],
        sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    with pytest.raises(LifecycleError) as captured:
        gateway.wait_for_idle(cancellation=cancellation, heartbeat=lambda _detail: None)

    assert captured.value.code == "lifecycle_cancelled"
    assert captured.value.exit_code == 10
