"""Nested settings/project/workflow reads cannot freeze other HTTP requests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.query_models import (
    JobsListView,
    ProjectWorkflowEntrypointsView,
    PublicProjectWorkflowCatalogView,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.admin.project_history import create_project_history_router
from core.entrypoints.admin.settings_retention import create_settings_retention_router
from core.entrypoints.api import auth_users, health, security, workload
from core.entrypoints.api.auth_tokens_accounts import create_auth_tokens_accounts_router
from core.entrypoints.api.events import create_api_events_router
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from core.entrypoints.api.jobs import create_api_jobs_router
from core.entrypoints.api.messages import create_api_messages_router
from core.entrypoints.api.project_integrations import create_project_integrations_router
from core.entrypoints.api.reports import create_api_reports_router
from core.entrypoints.api.system_maintenance import create_system_maintenance_router
from core.entrypoints.api.workflow_operator import create_workflow_operator_router
from core.infrastructure.security import policy_loader
from tests.support.api_read_probe import BlockedRead, probe_while_read_waits

_PROJECT_ID = "10000000-0000-0000-0000-000000000001"


def _error(exc, *_args, **_kwargs):
    return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 500))


def _scope(*_args, **_kwargs):
    return ScopeContext(account_id=DEFAULT_ACCOUNT_ID, principal_type="admin")


def _view(**payload):
    return SimpleNamespace(dump=lambda: payload)


def _app(router):
    app = FastAPI()
    app.include_router(router)
    return app


def _retention(reader, _monkeypatch):
    store = SimpleNamespace(get_retention_settings=reader.returning({"action_logs_days": 30}))
    return _app(
        create_settings_retention_router(
            require_authenticated_user=lambda *_args, **_kwargs: {"roles": ["admin"]},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: store,
            request_scope=_scope,
            resolve_actor_username=lambda *_args, **_kwargs: "admin",
        )
    )


def _security(reader, monkeypatch):
    policy = SimpleNamespace(model_dump=lambda **_kwargs: {"enabled": True})
    monkeypatch.setattr(policy_loader, "load_global_policy", reader.returning(policy))
    monkeypatch.setattr(security, "get_posture_payload", reader.returning({"score": 100}))
    app = _app(security.router)

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.auth_user = {"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]}
        return await call_next(request)

    return app


def _integrations(reader, monkeypatch):
    # The real services remain responsible for their HTTP/scoping contracts;
    # only their storage boundary is replaced by the waiting dependency.
    from core.application.secrets.service import SecretCatalogService

    monkeypatch.setattr(SecretCatalogService, "list_secrets", reader.returning([]))
    service = SimpleNamespace(
        get_workflow_bindings=reader.returning(_view(project_id=_PROJECT_ID, workflows=[])),
        list_workflow_linked_projects=reader.returning(_view(projects=[])),
    )
    return _app(
        create_integrations_connectors_router(
            get_logger=reader.returning(None),
            get_extension_install_store=lambda: None,
            get_extension_runtime_store=lambda: None,
            extension_query_service=lambda: None,
            project_query_service=lambda: service,
            project_command_service=lambda: None,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
            err_not_found="Not found",
        )
    )


def _workflow(reader, _monkeypatch):
    service = SimpleNamespace(
        list_project_workflows=reader.returning(
            PublicProjectWorkflowCatalogView(project_id=_PROJECT_ID)
        ),
        list_project_entrypoints=reader.returning(
            ProjectWorkflowEntrypointsView(project_id=_PROJECT_ID)
        ),
        workflow_stats=reader.returning({"workflow_id": "demo", "total_runs": 0}),
    )
    return _app(
        create_workflow_operator_router(
            workflow_operator_service=lambda *_args: service,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
        )
    )


def _history(reader, _monkeypatch):
    service = SimpleNamespace(
        list_history=reader.returning(_view(history=[])),
        get_history_diff=reader.returning(_view(changes=[])),
    )
    return _app(
        create_project_history_router(
            project_query_service=lambda: service,
            project_command_service=lambda: None,
            request_scope=_scope,
            resolve_actor_username=lambda *_args, **_kwargs: "admin",
            service_error_response=_error,
            api_error_response=_error,
        )
    )


def _jobs(reader, _monkeypatch):
    service = SimpleNamespace(
        list_jobs=reader.returning(JobsListView()),
        get_trace=reader.returning(_view(trace=[])),
        get_job_definition=reader.returning(_view(definition={})),
    )
    return _app(
        create_api_jobs_router(
            job_query_service=lambda: service,
            job_command_service=lambda: None,
            approval_query_service=lambda: None,
            request_scope=_scope,
            resolve_user_role=lambda _request: "admin",
            resolve_actor_username=lambda *_args, **_kwargs: "admin",
            optional_actor_username=lambda *_args, **_kwargs: "admin",
            service_error_response=_error,
            api_error_response=_error,
            get_logger_only=lambda: None,
            watchdog_scan=lambda *_args, **_kwargs: {},
            parse_worker_pid=lambda _value: None,
            terminate_worker_pid=lambda *_args, **_kwargs: {},
            append_runtime_journal=lambda *_args, **_kwargs: None,
            build_rerun_workflow_selection=lambda *_args, **_kwargs: ({}, {}),
            load_replay_snapshot=lambda _value: {},
            enqueue_task=lambda *_args, **_kwargs: 1,
            retryable_interrupted_step_status="interrupted",
            err_database_not_configured="Database not configured",
        )
    )


def _accounts(reader, _monkeypatch):
    store = SimpleNamespace(
        list_accounts=reader.returning([]),
        list_service_accounts=reader.returning([]),
        list_user_pats=reader.returning([]),
    )
    return _app(
        create_auth_tokens_accounts_router(
            local_auth_enabled=lambda: True,
            get_logger_only=lambda: store,
            auth_tables_available=lambda _store: True,
            auth_pat_tables_available=lambda _store: True,
            auth_account_tables_available=lambda _store: True,
            auth_service_account_tables_available=lambda _store: True,
            auth_machine_token_tables_available=lambda _store: True,
            require_authenticated_user=lambda *_args, **_kwargs: {
                "id": "admin",
                "account_id": DEFAULT_ACCOUNT_ID,
                "roles": ["admin"],
            },
            api_error_response=_error,
            log_auth_audit_event=lambda *_args, **_kwargs: None,
            auth_admin_roles={"admin"},
            err_local_auth_disabled="Local auth disabled",
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth unavailable",
            err_pat_tables_not_available="Tokens unavailable",
            err_authentication_required="Authentication required",
            err_token_not_found="Token not found",
            err_account_tables_not_available="Accounts unavailable",
            err_admin_role_required="Admin role required",
            err_service_account_key_required="Account key required",  # pragma: allowlist secret
            err_machine_token_tables_not_available="Machine tokens unavailable",
        )
    )


def _users(reader, monkeypatch):
    user = {"id": "admin", "account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]}
    store = object()
    monkeypatch.setattr(auth_users._preferences, "session_preferences", reader.returning({}))
    monkeypatch.setattr(auth_users, "_auth_email_change_tables_available", lambda _store: True)
    monkeypatch.setattr(
        auth_users.user_profile_repository, "get_pending_email_change", reader.returning(None)
    )
    queries = SimpleNamespace(list_users=reader.returning(SimpleNamespace(users=[], total=0)))
    router = auth_users.create_auth_users_router(
        local_auth_enabled=lambda: True,
        external_auth_enabled=lambda: False,
        get_logger_only=lambda: store,
        auth_tables_available=lambda _store: True,
        require_authenticated_user=lambda *_args, **_kwargs: user,
        request_auth_user_payload=lambda _request: user,
        api_error_response=_error,
        log_auth_audit_event=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures=lambda *_args, **_kwargs: None,
        get_user_by_id=lambda *_args: None,
        get_user_by_username=lambda *_args: None,
        normalize_requested_roles=lambda roles: roles or [],
        seed_default_roles=lambda _store: None,
        password_hash=lambda _value: "unused",
        auth_user_column_exists=lambda *_args: True,
        real_dict_cursor_kwargs=dict,
        auth_admin_roles={"admin"},
        err_authentication_required="Authentication required",
        err_database_not_configured="Database not configured",
        err_auth_tables_not_available="Auth unavailable",
        err_admin_role_required="Admin role required",
        err_local_auth_disabled="Local auth disabled",
        err_invalid_json_body="Invalid JSON",
        auth_csrf_cookie_name="csrf",
        user_query_service=queries,
    )
    app = _app(router)

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.auth_user = user
        return await call_next(request)

    return app


def _project_integrations(reader, _monkeypatch):
    store = SimpleNamespace(list_providers=reader.returning([]))
    service = SimpleNamespace(
        list_notification_routes=reader.returning(_view(routes=[])),
        get_notification_overview=reader.returning(_view(notifications=[])),
    )
    return _app(
        create_project_integrations_router(
            get_logger_only=lambda: store,
            project_query_service=lambda: service,
            project_command_service=lambda: None,
            request_scope=_scope,
            require_authenticated_user=lambda *_args, **_kwargs: {"roles": ["admin"]},
            to_json_obj=lambda value: value,
            api_error_response=_error,
            service_error_response=_error,
            secret_storage_config_error=lambda _exc: False,
            auth_admin_roles={"admin"},
            err_database_not_configured="Database not configured",
            err_project_not_found="Project not found",
            err_admin_role_required="Admin role required",
        )
    )


def _events(reader, _monkeypatch):
    service = SimpleNamespace(
        list_events=reader.returning(_view(events=[], total=0)),
        get_event_detail=reader.returning(_view(event={"event_id": "example"})),
    )
    return _app(
        create_api_events_router(
            event_query_service=lambda: service,
            event_command_service=lambda: None,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
        )
    )


def _technical_reads(reader, monkeypatch):
    monkeypatch.setattr(health, "_operations_request_authorized", lambda _request: True)
    monkeypatch.setattr(health, "_metrics_request_authorized", lambda _request: True)
    monkeypatch.setattr(health.deps, "get_logger_only", reader.returning(None))
    app = _app(health.router)
    app.include_router(workload.router)
    app.include_router(
        create_system_maintenance_router(status_provider=reader.returning({"status": "disabled"}))
    )
    return app


def _persisted_details(reader, _monkeypatch):
    service = SimpleNamespace(
        get_message_detail=reader.returning(_view(message={"id": "message-1"})),
        report_get=reader.returning({"report_id": "report-1"}),
    )
    app = _app(
        create_api_messages_router(
            job_query_service=lambda: service,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
        )
    )
    app.include_router(
        create_api_reports_router(
            reporting_service=lambda: service,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
        )
    )
    return app


@pytest.mark.parametrize(
    ("surface", "path", "status"),
    [
        ("retention", "/api/settings/retention", 200),
        ("technical_reads", "/health/deep", 503),
        ("technical_reads", "/metrics", 200),
        ("technical_reads", "/api/workload", 503),
        ("technical_reads", "/api/system/maintenance", 200),
        ("persisted_details", "/api/messages/message-1", 200),
        ("persisted_details", "/api/reports/report-1", 200),
        ("events", "/api/events", 200),
        ("events", "/api/events/example", 200),
        ("security", "/api/security/policy", 200),
        ("security", "/api/security/posture", 200),
        ("integrations", f"/api/projects/{_PROJECT_ID}/connections", 404),
        ("integrations", f"/api/projects/{_PROJECT_ID}/workflows", 200),
        ("integrations", "/api/workflows/demo/linked-projects", 200),
        ("integrations", "/api/secrets", 200),
        ("workflow", f"/api/projects/{_PROJECT_ID}/workflow-catalog", 200),
        ("workflow", f"/api/projects/{_PROJECT_ID}/workflow-entrypoints", 200),
        ("workflow", f"/api/projects/{_PROJECT_ID}/workflows/demo/stats", 200),
        ("history", f"/api/projects/{_PROJECT_ID}/history", 200),
        ("history", f"/api/projects/{_PROJECT_ID}/history/rev-1/diff", 200),
        ("jobs", "/api/jobs", 200),
        ("jobs", "/api/jobs/job-1/trace", 200),
        ("jobs", "/api/jobs/job-1/definition", 200),
        ("accounts", "/api/auth/accounts", 200),
        ("accounts", "/api/auth/service-accounts", 200),
        ("accounts", "/api/auth/tokens", 200),
        ("users", "/api/auth/me", 200),
        ("users", "/api/auth/users", 200),
        ("users", "/api/users/me/email-change-status", 200),
        ("project_integrations", "/api/providers", 200),
        ("project_integrations", f"/api/projects/{_PROJECT_ID}/notification-routes", 200),
        ("project_integrations", f"/api/projects/{_PROJECT_ID}/notification-overview", 200),
    ],
)
def test_nested_read_does_not_hold_up_other_requests(surface, path, status, monkeypatch):
    reader = BlockedRead()
    builders = {
        "retention": _retention,
        "technical_reads": _technical_reads,
        "persisted_details": _persisted_details,
        "events": _events,
        "security": _security,
        "integrations": _integrations,
        "workflow": _workflow,
        "history": _history,
        "jobs": _jobs,
        "accounts": _accounts,
        "users": _users,
        "project_integrations": _project_integrations,
    }
    app = builders[surface](reader, monkeypatch)
    heartbeat, response = asyncio.run(probe_while_read_waits(app, path, reader))
    assert response.status_code == status, response.text
    assert heartbeat.json() == {"read_finished": False}, (
        f"{path} blocked an unrelated HTTP request until its synchronous read ended"
    )
