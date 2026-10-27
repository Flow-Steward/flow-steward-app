from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from inspect import signature
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import (
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobToggleInput,
)
from core.application.management.command_services import ProjectCommandService
from core.infrastructure.db.logger_scheduled_jobs_mixin import (
    PlatformLoggerScheduledJobsMixin,
)


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.last_statement = ""
        self.rowcount = 1

    def execute(self, statement: str, params: Any = (), **_kwargs: Any) -> None:
        self.last_statement = " ".join(str(statement).split())
        self.executions.append((self.last_statement, tuple(params or ())))

    def fetchone(self) -> dict[str, Any]:
        return {
            "id": "schedule-1",
            "project_id": "project-1",
            "account_id": "account-1",
            "workflow_id": "wf-1",
            "admission_allowed": True,
            "admission_reason": "validated",
            "admission_workflow_revision": 4,
            "current_workflow_revision": 4,
        }

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "schedule-1",
                "project_id": "project-1",
                "account_id": "account-1",
                "workflow_id": "wf-1",
                "admission_allowed": True,
                "admission_reason": "validated",
                "admission_workflow_revision": 4,
                "current_workflow_revision": 4,
            }
        ]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    @contextmanager
    def cursor(self, **_kwargs: Any):
        yield self._cursor


class _Store(PlatformLoggerScheduledJobsMixin):
    def __init__(self) -> None:
        self.cursor = _Cursor()
        self.conn = _Connection(self.cursor)

    def _table_exists(self, _table_name: str) -> bool:
        return True


def test_create_scheduled_job_persists_admission_fields() -> None:
    store = _Store()
    checked_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    created = store.create_scheduled_job(
        account_id="account-1",
        project_id="project-1",
        workflow_id="wf-1",
        cron_expr="0 2 * * *",
        admission_allowed=True,
        admission_reason="validated",
        admission_workflow_revision=4,
        admission_checked_at=checked_at,
    )

    sql, params = store.cursor.executions[-1]
    assert created
    assert created["admission_workflow_revision"] == 4
    assert "admission_allowed" in sql
    assert "admission_reason" in sql
    assert "admission_workflow_revision" in sql
    assert "admission_checked_at" in sql
    assert params[-5:] == (True, "validated", 4, checked_at, "operator")


def test_schedule_edit_updates_inputs_timezone_with_project_account_scope() -> None:
    store = _Store()
    updated = store.update_scheduled_job(
        "project-1",
        "schedule-1",
        account_id="account-1",
        inputs={"region": "us"},
        schedule_timezone="Europe/Vilnius",
    )
    assert updated
    sql, params = store.cursor.executions[-1]
    assert "inputs = %s::jsonb" in sql
    assert "schedule_timezone = %s" in sql
    assert params[:2] == ('{"region": "us"}', "Europe/Vilnius")
    assert params[-4:] == ("project-1", "schedule-1", "account-1", "account-1")


def test_due_schedule_reads_current_aggregate_revision() -> None:
    store = _Store()

    rows = store.list_due_scheduled_jobs(now=datetime(2026, 8, 29, tzinfo=UTC))

    sql, _params = store.cursor.executions[-1]
    assert "LEFT JOIN registry_workflows rw ON rw.workflow_id = sj.workflow_id" in sql
    assert "rw.revision AS current_workflow_revision" in sql
    assert rows[0]["current_workflow_revision"] == 4


def test_claimed_due_schedule_keeps_current_aggregate_revision() -> None:
    store = _Store()
    scheduled_for = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    claimed = store.claim_due_scheduled_job(
        project_id="project-1",
        scheduled_job_id="schedule-1",
        expected_next_run_at=scheduled_for,
        next_run_at=datetime(2026, 8, 29, 12, 5, tzinfo=UTC),
        now=scheduled_for,
    )

    sql, _params = store.cursor.executions[-1]
    assert "WITH claimed AS" in sql
    assert "LEFT JOIN registry_workflows rw ON rw.workflow_id = claimed.workflow_id" in sql
    assert claimed
    assert claimed["current_workflow_revision"] == 4


class _SignatureCheckedScheduleStore:
    """In-memory state behind the production create/update method signatures."""

    def __init__(self) -> None:
        self.row: dict[str, Any] = {}

    def get_project(self, project_id: Any) -> dict[str, Any]:
        return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "name": "Demo"}

    def get_project_run_config(self, _project_id: Any) -> dict[str, Any]:
        return {
            "project_name": "Demo",
            "project_root": "/workspace/demo",
            "project_workflows": [{"workflow_id": "wf-1", "enabled": True}],
        }

    def create_scheduled_job(self, **kwargs: Any) -> dict[str, Any]:
        signature(PlatformLoggerScheduledJobsMixin.create_scheduled_job).bind(self, **kwargs)
        self.row = {
            "id": "schedule-1",
            "current_workflow_revision": kwargs["admission_workflow_revision"],
            **kwargs,
        }
        return dict(self.row)

    def get_scheduled_job(
        self, project_id: str, scheduled_job_id: str, *, account_id: str | None = None
    ) -> dict[str, Any]:
        _ = (project_id, scheduled_job_id, account_id)
        return dict(self.row)

    def list_scheduled_jobs(self, project_id, *, enabled_only=False, account_id=None):
        if (
            not self.row
            or self.row.get("project_id") != project_id
            or self.row.get("account_id") != account_id
        ):
            return []
        return [dict(self.row)] if not enabled_only or self.row.get("enabled") else []

    def update_scheduled_job(
        self, project_id: str, scheduled_job_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        signature(PlatformLoggerScheduledJobsMixin.update_scheduled_job).bind(
            self, project_id, scheduled_job_id, **kwargs
        )
        self.row.update(kwargs)
        if "admission_workflow_revision" in kwargs:
            self.row["current_workflow_revision"] = kwargs["admission_workflow_revision"]
        return dict(self.row)


@pytest.mark.parametrize(
    "expected_inputs",
    [
        [{"name": "region", "type": "string", "default": "eu"}],
        [
            {
                "name": "credential",
                "type": "secret",
                "default_secret_ref": "workflow-input:v1:owner:revision-1",
            }
        ],
    ],
    ids=["ordinary-default", "secret-default"],
)
def test_schedule_create_pause_enable_and_run_now_keep_caller_intent(
    expected_inputs: list[dict[str, Any]],
) -> None:
    store = _SignatureCheckedScheduleStore()
    launched: list[dict[str, Any]] = []
    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda *_args, **_kwargs: ("wf-1", "explicit_request"),
        validate_explicit_workflow_override=lambda **_kwargs: None,
        sanitize_external_input=lambda value: str(value).strip(),
        normalize_cron_expr=lambda value: str(value).strip(),
        cron_next_run_at=lambda *_args, **_kwargs: datetime(2026, 9, 6, tzinfo=UTC),
        form_flag=lambda value: str(value).strip().lower() in {"1", "true", "on"},
        run_scheduled_job_once=lambda _store, row, **_kwargs: (
            launched.append(dict(row)) or {"status": "enqueued", "job_id": "job-1"}
        ),
    )
    registry = SimpleNamespace(
        db_store=SimpleNamespace(
            get_workflow=lambda _workflow_id: {
                "revision": 4,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["schedule"],
                        "expected_inputs": expected_inputs,
                        "output_mode": "none",
                    }
                },
            }
        )
    )

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        service.create_scheduled_job(
            "project-1",
            ProjectScheduledJobCreateInput(
                workflow_id="wf-1",
                cron_expr="0 2 * * *",
                inputs={},
            ),
        )
        assert store.row["inputs"] == {}
        service.toggle_scheduled_job(
            "project-1", "schedule-1", ProjectScheduledJobToggleInput(enabled=False)
        )
        service.toggle_scheduled_job(
            "project-1", "schedule-1", ProjectScheduledJobToggleInput(enabled=True)
        )
        service.run_scheduled_job_now("project-1", "schedule-1")

    assert store.row["inputs"] == {}
    assert launched[-1]["inputs"] == {}
    assert "_workflow_secret_ref_fingerprints" not in store.row


def _schedule_edit_context():
    from core.application.workflows.serve_workflow_helpers import cron_next_run_at

    store = _SignatureCheckedScheduleStore()
    service = ProjectCommandService(
        get_logger=lambda: store,
        resolve_workflow_selection=lambda *_args, **_kwargs: ("wf-1", "explicit_request"),
        validate_explicit_workflow_override=lambda **_kwargs: None,
        sanitize_external_input=lambda value: str(value).strip(),
        normalize_cron_expr=lambda value: str(value).strip(),
        cron_next_run_at=cron_next_run_at,
        form_flag=bool,
    )
    registry = SimpleNamespace(
        db_store=SimpleNamespace(
            get_workflow=lambda _id: {
                "revision": 4,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["schedule"],
                        "expected_inputs": [{"name": "region", "type": "string", "required": True}],
                        "output_mode": "none",
                    }
                },
            }
        )
    )
    store.row = {
        "id": "schedule-1",
        "project_id": "project-1",
        "account_id": DEFAULT_ACCOUNT_ID,
        "workflow_id": "wf-1",
        "cron_expr": "0 2 * * *",
        "schedule_timezone": "UTC",
        "inputs": {"region": "eu"},
        "enabled": False,
    }
    return store, service, registry


def test_schedule_admission_keeps_empty_caller_inputs_without_task_envelope_fallback() -> None:
    store, service, registry = _schedule_edit_context()
    registry.db_store.get_workflow = lambda _id: {"revision": 4, "settings": {}}
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        return_value=registry,
    ):
        admission, inputs = service._scheduled_job_admission(
            workflow_id="wf-1",
            project=store.get_project("project-1"),
            project_config=store.get_project_run_config("project-1"),
            inputs={},
        )
    assert admission["admission_allowed"] is True
    assert inputs == {}


@pytest.mark.parametrize("schedule_timezone, expected_hour", [("Europe/Vilnius", 6), ("", 9)])
def test_enable_schedule_uses_own_timezone_before_project_default(schedule_timezone, expected_hour):
    store, service, registry = _schedule_edit_context()
    store.row.update(cron_expr="0 9 * * *", schedule_timezone=schedule_timezone)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 29, tzinfo=UTC)

    with (
        patch(
            "core.application.management.command_services_project_run_requests_mixin.datetime",
            FrozenDatetime,
        ),
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
    ):
        result = service.toggle_scheduled_job(
            "project-1", "schedule-1", ProjectScheduledJobToggleInput(enabled=True)
        )

    assert result.enabled is True
    assert store.row["next_run_at"] == datetime(2026, 3, 29, expected_hour, tzinfo=UTC)
    assert store.row["schedule_timezone"] == schedule_timezone


@pytest.mark.parametrize("schedule_timezone, expected_hour", [("Europe/Vilnius", 6), ("", 7)])
def test_project_timezone_edit_preserves_explicit_schedule_timezone(
    schedule_timezone, expected_hour
):
    from core.application.management.scope import ScopeContext

    store, service, _registry = _schedule_edit_context()
    store.row.update(
        cron_expr="0 9 * * *", schedule_timezone=schedule_timezone, enabled=True, status="active"
    )
    cursor = _Cursor()
    store.conn = SimpleNamespace(cursor=_Connection(cursor).cursor, commit=lambda: None)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID, allowed_project_ids=frozenset({"project-1"})
    )
    client = _schedule_api_client(store, service, scope)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 29, tzinfo=UTC)

    with patch("core.entrypoints.admin.project_schedules.datetime", FrozenDatetime):
        response = client.patch(
            "/api/projects/project-1/schedule-timezone", json={"schedule_timezone": "Europe/Berlin"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["recalculated_jobs"] == 1
    assert cursor.executions[-1][1] == (
        datetime(2026, 3, 29, expected_hour, tzinfo=UTC),
        "schedule-1",
        "project-1",
    )
    assert store.row["schedule_timezone"] == schedule_timezone


def test_schedule_full_edit_preserves_paused_intent() -> None:
    from core.application.management import command_models

    store, service, registry = _schedule_edit_context()
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance", return_value=registry
    ):
        result = service.update_scheduled_job(
            "project-1",
            "schedule-1",
            command_models.ProjectScheduledJobUpdateInput(
                workflow_id="wf-1",
                cron_expr="0 8 * * 1",
                schedule_timezone="Europe/Vilnius",
                inputs={"region": "us"},
            ),
        )
    assert "task_summary" not in result.job
    assert store.row["inputs"] == {"region": "us"}
    assert store.row["schedule_timezone"] == "Europe/Vilnius"
    assert store.row["enabled"] is False
    assert store.row["next_run_at"] is None


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"cron_expr": "not cron"}, "cron"),
        ({"schedule_timezone": "Nowhere/Invalid"}, "timezone"),
        ({"inputs": {}}, "region"),
        ({"workflow_id": "wf-other"}, "workflow"),
    ],
)
def test_schedule_edit_rejects_invalid_fields_without_changing_stored_job(changes, message) -> None:
    from core.application.management import ServiceValidationError, command_models

    store, service, registry = _schedule_edit_context()
    before = dict(store.row)
    update = command_models.ProjectScheduledJobUpdateInput(
        **{
            "workflow_id": "wf-1",
            "cron_expr": "0 8 * * 1",
            **changes,
        }
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        pytest.raises(ServiceValidationError, match=f"(?i){message}"),
    ):
        service.update_scheduled_job("project-1", "schedule-1", update)
    assert store.row == before


def _schedule_api_client(store, service, scope, query_service=None):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from core.application.workflows.serve_workflow_helpers import (
        cron_human_summary,
        cron_next_run_at,
        normalize_cron_expr,
        parse_cron_expression,
    )
    from core.entrypoints.admin.project_schedules import create_project_schedules_router

    app = FastAPI()
    app.include_router(
        create_project_schedules_router(
            local_auth_enabled=lambda: False,
            external_auth_enabled=lambda: False,
            require_authenticated_user=lambda *_args: {},
            get_logger_only=lambda: store,
            request_scope=lambda *_args, **_kwargs: scope,
            project_command_service=lambda: service,
            project_query_service=lambda: query_service,
            resolve_actor_username=lambda *_args, **_kwargs: "operator",
            service_error_response=lambda exc, **_kwargs: JSONResponse(
                exc.payload, status_code=exc.status_code
            ),
            api_error_response=lambda exc, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            cron_next_run_at=cron_next_run_at,
            normalize_cron_expr=normalize_cron_expr,
            parse_cron_expression=parse_cron_expression,
            cron_human_summary=cron_human_summary,
            err_authentication_required="Authentication required",
            err_invalid_json_body="Invalid JSON",
            err_database_not_configured="No database",
            err_project_not_found="Project not found",
        )
    )
    return TestClient(app)


def test_schedule_edit_api_persists_explicit_workflow_and_rejects_foreign_context() -> None:
    from core.application.management.scope import ScopeContext

    store, service, registry = _schedule_edit_context()
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        allowed_project_ids=frozenset({"project-1"}),
        allowed_workflow_ids=frozenset({"wf-1"}),
    )
    client = _schedule_api_client(store, service, scope)
    body = {
        "workflow_id": "wf-1",
        "cron_expr": "0 8 * * *",
        "inputs": {"region": "us"},
        "schedule_timezone": "Europe/Vilnius",
    }
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance", return_value=registry
    ):
        response = client.put("/api/projects/project-1/scheduled-jobs/schedule-1", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["job"]["inputs"] == {"region": "us"}
        persisted = dict(store.row)
        assert (
            client.put("/api/projects/foreign/scheduled-jobs/schedule-1", json=body).status_code
            == 403
        )
        assert client.put(
            "/api/projects/project-1/scheduled-jobs/schedule-1",
            json={**body, "workflow_id": "wf-other"},
        ).status_code in {400, 403}
        assert store.row == persisted
    client.close()


@pytest.mark.parametrize(
    "timezone, expected_status", [("Europe/Vilnius", 200), ("Nowhere/Invalid", 400)]
)
def test_schedule_preview_validates_and_uses_selected_timezone(timezone, expected_status) -> None:
    from core.application.management.scope import ScopeContext

    store, service, _registry = _schedule_edit_context()
    with _schedule_api_client(
        store, service, ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    ) as client:
        response = client.get(
            "/api/projects/project-1/cron-preview",
            params={"cron_expr": "0 9 * * *", "schedule_timezone": timezone, "count": 2},
        )
    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json()["timezone"] == "Europe/Vilnius"
        assert len(response.json()["next_runs"]) == 2
        assert all(run.endswith("09:00") for run in response.json()["next_runs"])


@pytest.mark.parametrize(
    "timezone,cron_expr,reference,instants,wall_times",
    [
        (
            "UTC",
            "15 10 * * 1-5",
            "2026-10-02T12:00:00Z",
            ["2026-10-05T10:15:00Z", "2026-10-06T10:15:00Z"],
            ["2026-10-05 10:15", "2026-10-06 10:15"],
        ),
        (
            "Europe/Vilnius",
            "0 9 * * *",
            "2026-10-01T12:00:00Z",
            ["2026-10-02T06:00:00Z", "2026-10-03T06:00:00Z"],
            ["2026-10-02 09:00", "2026-10-03 09:00"],
        ),
        (
            "America/New_York",
            "0 9 * * *",
            "2026-10-01T12:00:00Z",
            ["2026-10-01T13:00:00Z", "2026-10-02T13:00:00Z"],
            ["2026-10-01 09:00", "2026-10-02 09:00"],
        ),
        (
            "Europe/Vilnius",
            "30 3 * * *",
            "2026-03-28T04:00:00Z",
            ["2026-03-29T01:30:00Z", "2026-03-30T00:30:00Z"],
            ["2026-03-29 04:30", "2026-03-30 03:30"],
        ),
        (
            "Europe/Vilnius",
            "30 3 * * *",
            "2026-10-24T04:00:00Z",
            ["2026-10-25T00:30:00Z", "2026-10-26T01:30:00Z"],
            ["2026-10-25 03:30", "2026-10-26 03:30"],
        ),
        (
            "America/New_York",
            "30 2 * * *",
            "2026-03-07T09:00:00Z",
            ["2026-03-08T07:30:00Z", "2026-03-09T06:30:00Z"],
            ["2026-03-08 03:30", "2026-03-09 02:30"],
        ),
        (
            "America/New_York",
            "30 1 * * *",
            "2026-10-31T09:00:00Z",
            ["2026-11-01T05:30:00Z", "2026-11-02T06:30:00Z"],
            ["2026-11-01 01:30", "2026-11-02 01:30"],
        ),
    ],
)
def test_cron_preview_adds_instants_without_changing_legacy_wall_times(
    monkeypatch, timezone, cron_expr, reference, instants, wall_times
) -> None:
    from core.application.management.query_models_project_pages import ProjectCronPreviewView
    from core.application.management.scope import ScopeContext

    fixed = datetime.fromisoformat(reference)

    class _Clock(datetime):
        @classmethod
        def now(_cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr("core.entrypoints.admin.project_schedules.datetime", _Clock)
    store, service, _registry = _schedule_edit_context()
    with _schedule_api_client(
        store, service, ScopeContext(account_id=DEFAULT_ACCOUNT_ID)
    ) as client:
        response = client.get(
            "/api/projects/project-1/cron-preview",
            params={"cron_expr": cron_expr, "schedule_timezone": timezone, "count": 2},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("next_runs_utc") == instants
    assert payload["next_runs"] == wall_times
    assert ProjectCronPreviewView.model_validate(payload).model_dump()["next_runs_utc"] == instants


@pytest.mark.parametrize("enabled", [False, True], ids=["paused", "enabled"])
def test_created_workflow_schedule_is_visible_in_owning_project_scheduled_jobs_api(enabled) -> None:
    from core.application.management.query_services import ProjectQueryService
    from core.application.management.scope import ScopeContext

    store, service, registry = _schedule_edit_context()
    store.row = {}
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID, allowed_project_ids=frozenset({"project-1"})
    )
    queries = ProjectQueryService(
        get_logger=lambda: store,
        serialize_projects_grid_row=dict,
        get_queue_depth=lambda _id: 0,
        get_ready_queue_depth=lambda: 0,
    )
    with (
        _schedule_api_client(store, service, scope, queries) as client,
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
    ):
        assert client.get("/api/projects/project-1/scheduled-jobs").json()["jobs"] == []
        created = client.post(
            "/api/projects/project-1/scheduled-jobs",
            json={
                "workflow_id": "wf-1",
                "cron_expr": "0 9 * * *",
                "schedule_timezone": "Europe/Vilnius",
                "inputs": {"region": "eu"},
                "enabled": enabled,
            },
        )
        assert created.status_code == 201, created.text
        listed = client.get("/api/projects/project-1/scheduled-jobs")
        assert listed.status_code == 200, listed.text
        (job,) = listed.json()["jobs"]
        assert job["id"] == created.json()["scheduled_job_id"]
        assert job["workflow_id"] == "wf-1"
        assert job["project_id"] == "project-1"
        assert job["inputs"] == {"region": "eu"}
        assert {
            "instructions",
            "concurrency_scope",
            "concurrency_key",
            "catchup_policy",
            "resolved_concurrency_key",
            "concurrency_summary",
            "task_summary",
        }.isdisjoint(job)
        assert job["enabled"] is enabled
        assert job["status"] == ("active" if enabled else "paused")
        assert bool(job["next_run_at_utc"]) is enabled
        assert client.get("/api/projects/foreign/scheduled-jobs").status_code == 403


@pytest.fixture(autouse=True)
def _unit_schedule_owner_fence(monkeypatch):
    # These in-memory command stores model a live workflow, not SQL locking.
    from contextlib import nullcontext

    monkeypatch.setattr(
        "core.application.management.command_services_project_run_requests_mixin.workflow_trigger_transaction",
        lambda *_args, **_kwargs: nullcontext(),
    )
