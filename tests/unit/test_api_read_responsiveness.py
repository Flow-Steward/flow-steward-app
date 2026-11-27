"""A slow synchronous page read must not stall unrelated requests.

The waiting dependency stands in for blocking database/filesystem I/O. The
assertion checks response ordering, never a machine-dependent latency limit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceError, ServiceNotFoundError
from core.application.management.query_models import ProjectsListView
from core.application.management.scope import ScopeContext
from core.entrypoints.admin import guardrails
from core.entrypoints.api import app_bootstrap, contracts_v2, system_monitoring
from core.entrypoints.api.integrations_extension_routes import register_extension_routes
from core.entrypoints.api.integrations_mcp_routes import register_mcp_routes
from core.entrypoints.api.projects import create_api_projects_router
from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router
from core.entrypoints.api.trash import create_api_trash_router
from tests.support.api_read_probe import (
    BlockedRead as _BlockedRead,
)
from tests.support.api_read_probe import (
    probe_while_read_waits as _probe_while_read_waits,
)
from tests.support.extension_icon import EXTENSION_ICON_PNG


def _error(exc, *_args, **_kwargs):
    return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 500))


def _scope(*_args, **_kwargs):
    return ScopeContext(account_id=DEFAULT_ACCOUNT_ID, principal_type="admin")


def _extension_app(reader):
    app = FastAPI()
    router = APIRouter()
    view = SimpleNamespace(dump=lambda: {"extensions": [], "total": 0})
    service = SimpleNamespace(
        list_extensions=reader.returning(view),
        get_extension_safe=reader.returning(view),
        get_extension_icon=reader.returning(EXTENSION_ICON_PNG),
        get_compiled_manifest_safe=reader.returning({"actions": []}),
    )
    register_extension_routes(
        api_error_response=_error,
        extension_binding_service=None,
        extension_query_service=lambda: service,
        get_extension_install_store=lambda: None,
        get_extension_runtime_store=lambda: None,
        get_logger=lambda: None,
        request_scope=_scope,
        router=router,
        service_error_response=_error,
    )
    app.include_router(router)
    app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: None,
            event_command_service=lambda: None,
            job_query_service=lambda: None,
            extension_query_service=lambda: service,
            extension_binding_service=lambda: None,
            tool_execution_service=lambda: None,
            command_dispatch_service=lambda: None,
            request_scope=_scope,
            service_error_response=_error,
            api_error_response=_error,
        )
    )
    return app


def _bootstrap_app(reader, monkeypatch):
    monkeypatch.setattr(app_bootstrap, "system_email_available", lambda: False)
    app = FastAPI()

    @app.middleware("http")
    async def _signed_in(request, call_next):
        # The entitlement read runs for a signed-in account only (ADR 0084).
        request.state.auth_user = {"id": "user-1", "account_id": DEFAULT_ACCOUNT_ID}
        return await call_next(request)

    app.include_router(
        app_bootstrap.create_api_app_bootstrap_router(
            request_principal=lambda _request: None,
            local_auth_enabled=lambda: True,
            local_auth_bootstrap_required=lambda: False,
            external_auth_enabled=False,
            current_app_edition=lambda: "community",
            platform_contract=lambda _key: SimpleNamespace(snapshot=reader.returning({})),
            build_client_i18n_payload=lambda *_args, **_kwargs: {"locale": "en"},
            api_error_response=_error,
        )
    )
    return app


def _v2_app(reader, monkeypatch):
    monkeypatch.setattr(
        contracts_v2.dashboard_metrics_api,
        "api_dashboard_summary",
        reader.returning({"status": "ok"}),
    )
    app = FastAPI()
    service = SimpleNamespace(
        list_projects=reader.returning(ProjectsListView()),
        list_jobs=reader.returning({"jobs": [], "total": 0}),
    )
    app.include_router(
        contracts_v2.create_api_contracts_v2_router(
            request_scope=_scope,
            resolve_user_role=lambda _request: "admin",
            job_query_service=lambda: service,
            job_command_service=lambda: None,
            project_query_service=lambda: service,
            require_authenticated_user=lambda *_args, **_kwargs: {
                "account_id": DEFAULT_ACCOUNT_ID,
                "roles": ["admin"],
            },
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            err_admin_role_required="Admin role required",
        )
    )
    return app


def _mcp_app(reader):
    app = FastAPI()
    router = APIRouter()
    register_mcp_routes(
        api_error_response=_error,
        err_not_found="Not found",
        get_logger=lambda: None,
        project_query_service=lambda: SimpleNamespace(
            list_projects=reader.returning(ProjectsListView())
        ),
        request_scope=_scope,
        router=router,
        service_error_response=_error,
    )
    app.include_router(router)
    return app


def _legacy_projects_app(reader):
    app = FastAPI()
    app.include_router(
        create_api_projects_router(
            job_query_service=lambda: None,
            job_command_service=lambda: None,
            project_query_service=lambda: SimpleNamespace(
                list_projects=reader.returning(ProjectsListView())
            ),
            project_command_service=lambda: None,
            request_scope=_scope,
            require_authenticated_user=lambda *_args, **_kwargs: {
                "account_id": DEFAULT_ACCOUNT_ID,
                "roles": ["admin"],
            },
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            auth_admin_roles={"admin"},
            resolve_actor_username=lambda *_args, **_kwargs: "admin",
            project_create_input_cls=dict,
            project_update_input_cls=dict,
            service_error_cls=ServiceError,
            service_not_found_error_cls=ServiceNotFoundError,
            service_error_response=_error,
            api_error_response=_error,
            err_admin_role_required="Admin role required",
            err_database_not_configured="Database not configured",
            default_agent_models=dict,
            available_providers=list,
            available_models=list,
        )
    )
    return app


def _guardrails_app(reader, monkeypatch):
    monkeypatch.setattr(
        guardrails, "_get_db", lambda: SimpleNamespace(list_guardrails=reader.returning([]))
    )
    monkeypatch.setattr(guardrails, "_get_extension_runtime_store", lambda _db: None)
    app = FastAPI()
    app.include_router(guardrails.router)
    return app


def _monitoring_app(reader, monkeypatch):
    monkeypatch.setattr(system_monitoring.deps, "get_logger_only", reader.returning(None))
    app = FastAPI()
    app.include_router(
        system_monitoring.create_api_system_monitoring_router(
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            require_authenticated_user=lambda *_args, **_kwargs: {
                "account_id": DEFAULT_ACCOUNT_ID,
                "roles": ["admin"],
            },
            auth_admin_roles={"admin"},
        )
    )
    return app


def _trash_app(reader):
    app = FastAPI()
    app.include_router(
        create_api_trash_router(
            require_authenticated_user=lambda *_args, **_kwargs: {
                "account_id": DEFAULT_ACCOUNT_ID,
                "roles": ["admin"],
            },
            auth_admin_roles={"admin"},
            get_logger_only=reader.returning(None),
            real_dict_cursor_kwargs=dict,
            request_scope=_scope,
            trash_lifecycle_service=lambda: None,
            service_error_response=_error,
            api_error_response=_error,
            err_admin_role_required="Admin role required",
            err_database_not_configured="Database not configured",
        )
    )
    return app


@pytest.mark.parametrize(
    ("surface", "path", "expected_status"),
    [
        ("bootstrap", "/api/app/bootstrap", 200),
        ("extensions", "/api/extensions", 200),
        ("extensions", "/api/extensions/acme.demo", 200),
        ("extensions", "/api/extensions/acme.demo/icon", 200),
        ("extensions", "/api/extensions/acme.demo/compiled-manifest", 200),
        ("v2", "/api/v2/projects", 200),
        ("v2", "/api/v2/jobs", 200),
        ("v2", "/api/v2/dashboard/summary", 200),
        ("projects", "/api/projects", 200),
        ("mcp", "/api/mcp", 200),
        ("guardrails", "/api/guardrails", 200),
        ("monitoring", "/api/system/monitoring/summary", 503),
        ("trash", "/api/trash", 503),
    ],
)
def test_page_read_keeps_unrelated_requests_responsive(surface, path, expected_status, monkeypatch):
    reader = _BlockedRead()
    builders = {
        "bootstrap": lambda: _bootstrap_app(reader, monkeypatch),
        "extensions": lambda: _extension_app(reader),
        "v2": lambda: _v2_app(reader, monkeypatch),
        "mcp": lambda: _mcp_app(reader),
        "projects": lambda: _legacy_projects_app(reader),
        "guardrails": lambda: _guardrails_app(reader, monkeypatch),
        "monitoring": lambda: _monitoring_app(reader, monkeypatch),
        "trash": lambda: _trash_app(reader),
    }
    heartbeat, response = asyncio.run(_probe_while_read_waits(builders[surface](), path, reader))
    assert response.status_code == expected_status, response.text
    assert heartbeat.json() == {"read_finished": False}, (
        f"{path} blocked unrelated requests until the synchronous read finished"
    )
