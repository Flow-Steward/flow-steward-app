from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_reported_worker_subprocesses_are_returned_when_proc_scan_is_blind(monkeypatch):
    from core.application.monitoring import processes as monitoring

    monkeypatch.setattr(
        monitoring,
        "load_reported_worker_subprocess_rows",
        lambda _job_ids: [
            {
                "job_id": "job-1",
                "agent": "composer",
                "status": "running",
                "timestamp": datetime.now(UTC),
                "payload": {
                    "pid": 4242,
                    "kind": "provider_request",
                    "cmdline": "python -m worker.provider_call",
                    "cwd": "/app/.flow-steward_worktrees/app/task-1",
                    "worker_id": "worker-1",
                },
            }
        ],
    )

    rows = monitoring._get_reported_worker_subprocesses([{"id": "job-1"}])

    assert rows[0]["pid"] == 4242
    assert rows[0]["reported"] is True
    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["cwd"] == "/app/.flow-steward_worktrees/app/task-1"


def test_reported_worker_subprocesses_keep_same_pid_from_different_workers(monkeypatch):
    from core.application.monitoring import processes as monitoring

    monkeypatch.setattr(
        monitoring,
        "load_reported_worker_subprocess_rows",
        lambda _job_ids: [
            {
                "job_id": "job-1",
                "agent": "composer",
                "status": "running",
                "timestamp": datetime.now(UTC),
                "payload": {"pid": 4242, "kind": "provider_request", "worker_id": "worker-1"},
            },
            {
                "job_id": "job-1",
                "agent": "proofreader",
                "status": "running",
                "timestamp": datetime.now(UTC),
                "payload": {"pid": 4242, "kind": "quality_check", "worker_id": "worker-2"},
            },
        ],
    )

    rows = monitoring._get_reported_worker_subprocesses([{"id": "job-1"}])

    assert len(rows) == 2
    assert {row["worker_id"] for row in rows} == {"worker-1", "worker-2"}
    assert [row["pid"] for row in rows] == [4242, 4242]


def test_list_processes_keeps_reported_row_when_pid_collides(monkeypatch):
    from core.application.monitoring import processes as monitoring

    monkeypatch.setattr(monitoring, "_get_active_jobs", lambda: [{"id": "job-1"}])
    monkeypatch.setattr(monitoring, "_get_active_project_leases", list)
    monkeypatch.setattr(
        monitoring,
        "_scan_worker_subprocesses",
        lambda _jobs: (
            [{"pid": 4242, "reported": False, "kind": "app_visible"}],
            {},
            set(),
        ),
    )
    monkeypatch.setattr(
        monitoring,
        "_get_reported_worker_subprocesses",
        lambda _jobs: [{"pid": 4242, "reported": True, "kind": "quality_check"}],
    )
    monkeypatch.setattr(monitoring, "_scan_orphan_processes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(monitoring, "_build_worker_lease_rows", lambda *_args: [])

    payload = monitoring.list_process_payload()

    assert len(payload["subprocesses"]) == 2
    assert any(row.get("reported") is True for row in payload["subprocesses"])


def test_list_processes_reaps_reported_rows_for_missing_worker_lease(monkeypatch):
    from core.application.monitoring import processes as monitoring

    calls: list[dict] = []

    def _fake_reap(jobs, leases):
        calls.append({"jobs": jobs, "leases": leases})
        return 1

    monkeypatch.setattr(
        monitoring,
        "_get_active_jobs",
        lambda: [{"id": "job-1", "worker_id": "worker-stale"}],
    )
    monkeypatch.setattr(monitoring, "_get_active_project_leases", list)
    monkeypatch.setattr(monitoring, "_reap_stale_reported_worker_subprocesses", _fake_reap)
    monkeypatch.setattr(monitoring, "_scan_worker_subprocesses", lambda _jobs: ([], {}, set()))
    monkeypatch.setattr(monitoring, "_get_reported_worker_subprocesses", lambda _jobs: [])
    monkeypatch.setattr(monitoring, "_scan_orphan_processes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(monitoring, "_build_worker_lease_rows", lambda *_args: [])

    payload = monitoring.list_process_payload()

    assert calls == [{"jobs": [{"id": "job-1", "worker_id": "worker-stale"}], "leases": []}]
    assert payload["reaped_worker_subprocess_count"] == 1


def test_reap_stale_reported_worker_subprocesses_uses_missing_worker_lease(monkeypatch):
    from core.application.monitoring import processes as monitoring

    calls: list[dict] = []

    def _fake_reap(stale_by_job):
        calls.append(stale_by_job)
        return 1

    monkeypatch.setattr(monitoring, "reap_stale_worker_reports", _fake_reap)

    count = monitoring._reap_stale_reported_worker_subprocesses(
        [{"id": "job-1", "worker_id": "worker-stale"}],
        [{"worker_instance_id": "worker-live"}],
    )

    assert count == 1
    assert calls == [{"job-1": {"worker-stale"}}]


def test_list_processes_does_not_reap_when_lease_evidence_unavailable(monkeypatch):
    from core.application.monitoring import processes as monitoring

    monkeypatch.setattr(
        monitoring,
        "_get_active_jobs",
        lambda: [{"id": "job-1", "worker_id": "worker-stale"}],
    )
    monkeypatch.setattr(
        monitoring,
        "_get_active_project_leases",
        lambda: monitoring._LEASE_EVIDENCE_UNAVAILABLE,
    )
    monkeypatch.setattr(monitoring, "_scan_worker_subprocesses", lambda _jobs: ([], {}, set()))
    monkeypatch.setattr(monitoring, "_get_reported_worker_subprocesses", lambda _jobs: [])
    monkeypatch.setattr(monitoring, "_scan_orphan_processes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(monitoring, "_build_worker_lease_rows", lambda *_args: [])

    payload = monitoring.list_process_payload()

    assert payload["reaped_worker_subprocess_count"] == 0
    assert payload["project_leases"] == []
    assert payload["project_lease_evidence_unavailable"] is True


def test_list_processes_orphan_scan_ignores_worker_reported_pid_namespace(monkeypatch):
    from core.application.monitoring import processes as monitoring

    captured: dict[str, set[int]] = {}

    monkeypatch.setattr(monitoring, "_get_active_jobs", lambda: [{"id": "job-1"}])
    monkeypatch.setattr(monitoring, "_get_active_project_leases", list)
    monkeypatch.setattr(
        monitoring,
        "_scan_worker_subprocesses",
        lambda _jobs: (
            [{"pid": 1111, "reported": False, "kind": "app_visible"}],
            {1111: {"pid": 1111}, 4242: {"pid": 4242, "cmdline": "stray-process"}},
            set(),
        ),
    )
    monkeypatch.setattr(
        monitoring,
        "_get_reported_worker_subprocesses",
        lambda _jobs: [{"pid": 4242, "reported": True, "kind": "quality_check"}],
    )

    def _fake_scan_orphans(_proc_index, _worker_pids, known_subprocess_pids):
        captured["known_subprocess_pids"] = set(known_subprocess_pids)
        return [{"pid": 4242, "kind": "stray-process"}]

    monkeypatch.setattr(monitoring, "_scan_orphan_processes", _fake_scan_orphans)
    monkeypatch.setattr(monitoring, "_build_worker_lease_rows", lambda *_args: [])

    payload = monitoring.list_process_payload()

    assert captured["known_subprocess_pids"] == {1111}
    assert any(row.get("pid") == 4242 for row in payload["subprocesses"])
    assert any(row.get("pid") == 4242 for row in payload["suspected_orphans"])


def _process_route_client(auth_user: dict[str, object] | None) -> TestClient:
    from core.entrypoints.api.processes import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject_auth_user(request: Request, call_next):
        if auth_user is not None:
            request.state.auth_user = auth_user
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_kill_process_route_requires_admin_user() -> None:
    client = _process_route_client({"id": 1, "roles": ["operator"]})

    response = client.post("/api/processes/123/kill")

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin role required"


def test_kill_process_route_rejects_missing_auth_user() -> None:
    client = _process_route_client(None)

    response = client.post("/api/processes/123/kill")

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin role required"


def test_kill_process_route_calls_service_for_admin(monkeypatch) -> None:
    from core.entrypoints.api import processes as process_routes

    monkeypatch.setattr(
        process_routes,
        "terminate_process",
        lambda pid: {"ok": True, "pid": pid},
    )
    client = _process_route_client({"id": 1, "roles": ["admin"]})

    response = client.post("/api/processes/123/kill")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pid": 123}


def test_worker_processes_are_reported_without_an_active_job(monkeypatch: pytest.MonkeyPatch):
    """AC17-R69: a healthy Compact install reported "Worker processes: 0".

    `_scan_worker_subprocesses` derives worker PIDs from **active jobs** and returns nothing
    when none are running, so the panel could only ever show children of a running job. On the
    owner's server two workers were alive as PIDs 30 and 31 while the page said zero, and
    `FS_WORKER_PROCESSES` — the number the installer asks the operator for — could not be
    confirmed to have taken effect.
    """
    from core.application.monitoring import processes as monitoring

    index = {
        1: {
            "pid": 1,
            "ppid": 0,
            "cmdline": "python -m core.entrypoints.cli.compact_runtime",
            "rss_mb": 40.0,
        },
        12: {
            "pid": 12,
            "ppid": 1,
            "cmdline": "python -m uvicorn core.entrypoints.api.app",
            "rss_mb": 90.0,
        },
        30: {
            "pid": 30,
            "ppid": 1,
            "cmdline": "python -m core.entrypoints.cli.worker_process",
            "rss_mb": 120.5,
        },
        31: {
            "pid": 31,
            "ppid": 1,
            "cmdline": "python -m core.entrypoints.cli.worker_process",
            "rss_mb": 118.0,
        },
    }

    rows = monitoring._scan_worker_processes(index)

    assert [row["pid"] for row in rows] == [30, 31]
    assert {row["kind"] for row in rows} == {"worker"}
    # the supervisor and the API are not workers
    assert all(row["pid"] not in (1, 12) for row in rows)


def test_worker_process_scan_is_empty_where_workers_live_elsewhere():
    """On Scalable the workers are in their own container and genuinely invisible.

    Reporting nothing is correct there; it must not be mistaken for a failure, and the scan
    must not start matching the supervisor or the API instead.
    """
    from core.application.monitoring import processes as monitoring

    index = {
        1: {
            "pid": 1,
            "ppid": 0,
            "cmdline": "python -m uvicorn core.entrypoints.api.app",
            "rss_mb": 90.0,
        },
    }

    assert monitoring._scan_worker_processes(index) == []
