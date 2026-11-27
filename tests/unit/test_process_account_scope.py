"""``GET /api/processes`` shows a caller only the account they act in (ADR 0082 s. 8).

Workers are shared by every account of an installation. The installation
administrator sees every account's jobs; an operator or an account administrator
sees the jobs, leases and attributable subprocesses of their own account, and
nothing installation-wide.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.monitoring import processes as monitoring

OTHER_ACCOUNT = "acme.second-account"
OWN_PROJECT = "11111111-1111-1111-1111-111111111111"
OTHER_PROJECT = "22222222-2222-2222-2222-222222222222"
PROJECT_ACCOUNTS = {OWN_PROJECT: OTHER_ACCOUNT, OTHER_PROJECT: DEFAULT_ACCOUNT_ID}

INSTALLATION_ADMIN: dict[str, Any] = {
    "id": 1,
    "username": "root-admin",
    "roles": ["admin"],
    "account_id": DEFAULT_ACCOUNT_ID,
}
ACCOUNT_ADMIN: dict[str, Any] = {
    "id": 2,
    "username": "acme-admin",
    "roles": ["admin"],
    "account_id": OTHER_ACCOUNT,
}
ACCOUNT_OPERATOR: dict[str, Any] = {
    "id": 3,
    "username": "acme-operator",
    "roles": ["operator"],
    "account_id": OTHER_ACCOUNT,
}
INSTALLATION_OPERATOR: dict[str, Any] = {
    "id": 4,
    "username": "operator",
    "roles": ["operator"],
    "account_id": DEFAULT_ACCOUNT_ID,
}


def _payload() -> dict[str, Any]:
    return {
        "jobs": [
            {"id": "job-own", "project_id": OWN_PROJECT, "worker_id": "worker-host-100"},
            {"id": "job-other", "project_id": OTHER_PROJECT, "worker_id": "worker-host-200"},
            {"id": "job-shared-own", "project_id": OWN_PROJECT, "worker_id": "worker-host-300"},
            {"id": "job-shared-other", "project_id": OTHER_PROJECT, "worker_id": "worker-host-300"},
        ],
        "subprocesses": [
            {"pid": 101, "worker_pid": 100, "cmdline": "own child"},
            {"pid": 201, "worker_pid": 200, "cmdline": "other child"},
            {"pid": 301, "worker_pid": 300, "cmdline": "shared worker child"},
            {"pid": 900, "job_id": "job-own", "reported": True, "cmdline": "own report"},
            {"pid": 901, "job_id": "job-other", "reported": True, "cmdline": "other report"},
        ],
        "suspected_orphans": [{"pid": 5, "cmdline": "orphan"}],
        "project_leases": [
            {"project_id": OWN_PROJECT, "worker_instance_id": "worker-host-100"},
            {"project_id": OTHER_PROJECT, "worker_instance_id": "worker-host-200"},
        ],
        "project_lease_evidence_unavailable": False,
        "workers": [
            {"worker_instance_id": "worker-host-100"},
            {"worker_instance_id": "worker-host-200"},
        ],
        "worker_processes": [{"pid": 100, "kind": "worker"}],
        "reaped_worker_subprocess_count": 2,
    }


@pytest.fixture
def project_accounts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    asked: list[list[str]] = []

    def load(project_ids: list[str]) -> dict[str, str]:
        asked.append(list(project_ids))
        return {key: PROJECT_ACCOUNTS[key] for key in project_ids if key in PROJECT_ACCOUNTS}

    monkeypatch.setattr(monitoring, "load_project_account_ids", load)
    return asked


def test_scoping_keeps_only_the_accounts_work(project_accounts: list[list[str]]) -> None:
    scoped = monitoring.scope_process_payload_to_account(_payload(), OTHER_ACCOUNT)

    assert [job["id"] for job in scoped["jobs"]] == ["job-own", "job-shared-own"]
    assert [lease["project_id"] for lease in scoped["project_leases"]] == [OWN_PROJECT]
    # The shared worker's child cannot be attributed, so it is not shown.
    assert sorted(row["pid"] for row in scoped["subprocesses"]) == [101, 900]
    assert [worker["worker_instance_id"] for worker in scoped["workers"]] == [
        "worker-host-100",
        "worker-host-300",
    ]
    assert scoped["suspected_orphans"] == []
    assert scoped["worker_processes"] == []
    assert scoped["reaped_worker_subprocess_count"] == 0
    assert project_accounts == [sorted({OWN_PROJECT, OTHER_PROJECT})]


def test_scoping_fails_closed_when_project_accounts_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_project_ids: list[str]) -> dict[str, str]:
        raise RuntimeError("database down")

    monkeypatch.setattr(monitoring, "load_project_account_ids", broken)

    scoped = monitoring.scope_process_payload_to_account(_payload(), OTHER_ACCOUNT)

    assert scoped["jobs"] == []
    assert scoped["project_leases"] == []
    assert scoped["subprocesses"] == []
    assert scoped["workers"] == []


def _client(auth_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from core.entrypoints.api import processes as process_routes

    monkeypatch.setattr(process_routes, "list_process_payload", _payload)
    app = FastAPI()
    app.include_router(process_routes.router)

    @app.middleware("http")
    async def _inject_auth_user(request: Request, call_next):
        request.state.auth_user = dict(auth_user)
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("caller", [ACCOUNT_OPERATOR, ACCOUNT_ADMIN])
def test_route_shows_an_account_user_only_their_account(
    caller: dict[str, Any], monkeypatch: pytest.MonkeyPatch, project_accounts: list[list[str]]
) -> None:
    response = _client(caller, monkeypatch).get("/api/processes")

    assert response.status_code == 200
    body = response.json()
    assert {job["id"] for job in body["jobs"]} == {"job-own", "job-shared-own"}
    assert {row["pid"] for row in body["subprocesses"]} == {101, 900}
    assert body["suspected_orphans"] == []
    assert body["worker_processes"] == []


def test_route_scopes_an_installation_account_operator_too(
    monkeypatch: pytest.MonkeyPatch, project_accounts: list[list[str]]
) -> None:
    response = _client(INSTALLATION_OPERATOR, monkeypatch).get("/api/processes")

    assert response.status_code == 200
    assert {job["id"] for job in response.json()["jobs"]} == {"job-other", "job-shared-other"}


def test_route_shows_the_installation_admin_every_account(
    monkeypatch: pytest.MonkeyPatch, project_accounts: list[list[str]]
) -> None:
    response = _client(INSTALLATION_ADMIN, monkeypatch).get("/api/processes")

    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 4
    assert len(body["subprocesses"]) == 5
    assert body["suspected_orphans"] == [{"pid": 5, "cmdline": "orphan"}]
    assert project_accounts == []
