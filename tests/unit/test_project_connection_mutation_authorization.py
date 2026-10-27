"""Authorization regressions for project-connection mutation routes."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from core.application.auth.auth_http_runtime import build_local_auth_middleware_handler
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.request_principal_runtime import roles_intersect
from core.application.management.command_services import ProjectConnectionService
from core.application.management.requests import (
    ProjectConnectionCreateRequest,
    ProjectConnectionSecretsSaveRequest,
    ProjectConnectionUpdateRequest,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from core.infrastructure.runtime import http_app_auth_runtime

PROJECT_ID = "00000000-0000-0000-0000-000000000042"
CONNECTION_ID = "conn_viewer_guard"


class _ConnectionState(TypedDict):
    connection: dict[str, Any]
    created: list[dict[str, Any]]
    deleted: bool


def _connection_row() -> dict[str, Any]:
    return {
        "connection_id": CONNECTION_ID,
        "project_id": PROJECT_ID,
        "extension_id": "acme.notifier",
        "account_id": DEFAULT_ACCOUNT_ID,
        "display_name": "Viewer guard",
        "connection_kind": "mcp_connector",
        "connection_type": "api_key",
        "environment": "test",
        "extension_install_id": "",
        "runtime_mode": None,
        "remote_base_url": "",
        "health_status": "",
        "runtime_status": "",
        "last_seen_ok_at": None,
        "last_error": "raw provider token=must-not-show-to-viewer",
        "enabled": False,
        "credential_revision": 7,
        "connection_config": {
            "oauth": {
                "lifecycle_status": "needs_attention",
                "last_error_code": "provider_unavailable",
                "authorization_intent_id": "  manager-only-intent  ",
                "connected_identity": {"email": "private@example.test"},
                "provider_detail": "manager-only-provider-detail",
                "reconnect_reason": "manager-only-reconnect-reason",
            },
            "arbitrary": "manager-only",
        },
        "secret_refs": {"refs": {"existing": "project_connection:conn_viewer_guard:existing"}},
        "secrets": {"api_key": "must-not-expose"},
        "connection_routes": [{"route": "manager-only"}],
        "ingress_config": {"path": "/manager-only"},
        "workflow_mappings": {"workflow": "manager-only"},
        "tags": [],
        "oauth_configuration_id": "oauth_config_1",
        "oauth_provider_id": "provider_1",
    }


def _service_and_state() -> tuple[ProjectConnectionService, MagicMock, _ConnectionState]:
    state: _ConnectionState = {
        "connection": _connection_row(),
        "created": [],
        "deleted": False,
    }
    store = MagicMock()
    store.get_project.return_value = {
        "id": PROJECT_ID,
        "account_id": DEFAULT_ACCOUNT_ID,
        "name": "Viewer guard project",
    }
    store.create_project_connection.side_effect = lambda row: state["created"].append(
        copy.deepcopy(row)
    )
    service = ProjectConnectionService(get_logger=lambda: store)
    service._extension_connection_schemas = lambda **_kwargs: ({}, {})
    service._oauth_platform_secret_ref_names = lambda **_kwargs: set()
    service._get_connection_row = lambda _bl, connection_id: (
        copy.deepcopy(state["connection"])
        if connection_id == CONNECTION_ID and not state["deleted"]
        else None
    )
    service._list_connection_rows = lambda _bl, project_id: (
        [copy.deepcopy(state["connection"])]
        if project_id == PROJECT_ID and not state["deleted"]
        else []
    )

    def _update(_bl, connection_id, updates):
        assert connection_id == CONNECTION_ID
        state["connection"].update(copy.deepcopy(updates))

    service._update_connection_row = _update
    service._invalidate_connection_sessions = MagicMock()
    return service, store, state


def _route_endpoint(method: str, path: str, *, store, scope):
    def _service_error_response(exc, *_args):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    router = create_integrations_connectors_router(
        get_logger=lambda: store,
        get_integration_store=lambda: store,
        get_extension_install_store=MagicMock(),
        get_extension_runtime_store=MagicMock(),
        extension_query_service=MagicMock(),
        request_scope=lambda request, **_kwargs: request.state.authenticated_scope,
        project_query_service=MagicMock(),
        project_command_service=MagicMock(),
        service_error_response=_service_error_response,
        api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
        err_not_found="Not found",
    )
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route.endpoint, SimpleNamespace(state=SimpleNamespace(authenticated_scope=scope))
    raise AssertionError(f"route {method} {path} was not registered")


def _authenticated_http_client(*, store, authenticated_user: dict) -> TestClient:
    app = FastAPI()
    auth_handler = build_local_auth_middleware_handler(
        local_auth_enabled_fn=lambda: True,
        external_auth_enabled=False,
        is_public_auth_path_fn=lambda _path: False,
        route_registered_fn=lambda _path, _method: True,
        request_principal_resolver_fn=lambda _request, _bl: None,
        normalize_live_principal_fn=http_app_auth_runtime._normalize_live_principal,
        get_logger_only_fn=lambda: store,
        auth_tables_available_fn=lambda _bl: True,
        get_user_by_session_token_fn=lambda _bl, token: (
            authenticated_user if token == "viewer-token" else None
        ),
        local_auth_bootstrap_required_fn=lambda _bl: False,
        auth_account_tables_available_fn=lambda _bl: False,
        roles_intersect_fn=roles_intersect,
        is_admin_only_route_fn=lambda _path: False,
        is_registry_read_route_fn=lambda _path: False,
        is_self_service_auth_write_route_fn=lambda _path: False,
        csrf_request_valid_fn=lambda _request: True,
        auth_cookie_name="fs_session",
        auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
        auth_admin_roles={"admin"},
        auth_write_roles={"operator", "admin"},
        err_database_not_configured="Database not configured",
        err_auth_tables_not_available="Auth tables unavailable",
        err_authentication_required="Authentication required",
        err_account_disabled="Account disabled",
        err_admin_role_required="Admin role required",
        err_operator_role_required="Operator role required",
        path_bootstrap="/setup",
        path_login="/login",
    )

    @app.middleware("http")
    async def _production_auth_middleware(request, call_next):
        return await auth_handler(request, call_next)

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: store,
            get_integration_store=lambda: store,
            get_extension_install_store=MagicMock(),
            get_extension_runtime_store=MagicMock(),
            extension_query_service=MagicMock(),
            request_scope=http_app_auth_runtime._request_scope,
            project_query_service=MagicMock(),
            project_command_service=MagicMock(),
            service_error_response=lambda exc, *_args: JSONResponse(
                {"error": str(exc)}, status_code=exc.status_code
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            err_not_found="Not found",
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _send_mutation(client: TestClient, mutation: str):
    headers = {"Authorization": "Bearer viewer-token"}
    if mutation == "create":
        return client.post(
            f"/api/projects/{PROJECT_ID}/connections",
            json={
                "extension_id": "acme.notifier",
                "connection_kind": "mcp_connector",
                "display_name": "Must not be created",
            },
            headers=headers,
        )
    if mutation == "update":
        return client.put(
            f"/api/projects/{PROJECT_ID}/connections/{CONNECTION_ID}",
            json={"enabled": True, "display_name": "Must not change"},
            headers=headers,
        )
    if mutation == "save_secrets":
        return client.post(
            f"/api/projects/{PROJECT_ID}/connections/{CONNECTION_ID}/secrets",
            json={"secrets": {"api_key": "must-not-persist"}},
            headers=headers,
        )
    return client.delete(f"/api/projects/{PROJECT_ID}/connections/{CONNECTION_ID}", headers=headers)


@pytest.mark.parametrize("mutation", ["create", "update", "save_secrets", "delete"])
def test_authenticated_viewer_cannot_mutate_project_connections(mutation):
    service, store, state = _service_and_state()
    before = copy.deepcopy(state)
    viewer_scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="authenticated-viewer",
        principal_type="viewer",
        auth_source="local",
    )

    with (
        patch(
            "core.application.management.command_services.ProjectConnectionService",
            return_value=service,
        ),
        patch(
            "core.application.management.project_connection_secrets.save_connection_secrets",
            return_value={"refs": {"api_key": "project_connection:conn_viewer_guard:api_key"}},
        ),
        patch(
            "core.application.management.command_services_project_connection.cleanup_project_connection_owned_data",
            side_effect=lambda *_args, **_kwargs: state.__setitem__("deleted", True),
        ),
    ):
        if mutation == "create":
            endpoint, request = _route_endpoint(
                "POST",
                "/api/projects/{project_id}/connections",
                store=store,
                scope=viewer_scope,
            )
            response = asyncio.run(
                endpoint(
                    request,
                    PROJECT_ID,
                    ProjectConnectionCreateRequest(
                        extension_id="acme.notifier",
                        connection_kind="mcp_connector",
                        display_name="Must not be created",
                    ),
                )
            )
        elif mutation == "update":
            endpoint, request = _route_endpoint(
                "PUT",
                "/api/projects/{project_id}/connections/{connection_id}",
                store=store,
                scope=viewer_scope,
            )
            response = asyncio.run(
                endpoint(
                    request,
                    PROJECT_ID,
                    CONNECTION_ID,
                    ProjectConnectionUpdateRequest(enabled=True, display_name="Must not change"),
                )
            )
        elif mutation == "save_secrets":
            endpoint, request = _route_endpoint(
                "POST",
                "/api/projects/{project_id}/connections/{connection_id}/secrets",
                store=store,
                scope=viewer_scope,
            )
            response = asyncio.run(
                endpoint(
                    request,
                    PROJECT_ID,
                    CONNECTION_ID,
                    ProjectConnectionSecretsSaveRequest(secrets={"api_key": "must-not-persist"}),
                )
            )
        else:
            endpoint, request = _route_endpoint(
                "DELETE",
                "/api/projects/{project_id}/connections/{connection_id}",
                store=store,
                scope=viewer_scope,
            )
            response = asyncio.run(endpoint(request, PROJECT_ID, CONNECTION_ID))

    assert getattr(response, "status_code", 200) == 403
    assert state == before
    assert state["connection"]["enabled"] is False
    assert state["connection"]["credential_revision"] == 7
    assert state["connection"]["secret_refs"] == {
        "refs": {"existing": "project_connection:conn_viewer_guard:existing"}
    }


@pytest.mark.parametrize("mutation", ["create", "update", "save_secrets", "delete"])
def test_authenticated_viewer_http_requests_leave_project_connections_unchanged(mutation):
    service, store, state = _service_and_state()
    before = copy.deepcopy(state)
    viewer_user = {
        "id": 17,
        "username": "viewer",
        "display_name": "Viewer",
        "is_active": True,
        "roles": ["viewer"],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    client = _authenticated_http_client(store=store, authenticated_user=viewer_user)

    with patch(
        "core.application.management.command_services.ProjectConnectionService",
        return_value=service,
    ):
        response = _send_mutation(client, mutation)

    assert response.status_code == 403
    assert state == before
    assert state["created"] == []
    assert state["deleted"] is False
    assert state["connection"]["enabled"] is False
    assert state["connection"]["credential_revision"] == 7
    assert state["connection"]["secret_refs"] == {
        "refs": {"existing": "project_connection:conn_viewer_guard:existing"}
    }


@pytest.mark.parametrize("read_operation", ["list", "get"])
@pytest.mark.parametrize(
    "lifecycle_status",
    ["needs_attention", "post_connect_pending", "disconnecting", "reconnect_required"],
)
def test_authenticated_viewer_http_requests_can_read_project_connections(
    read_operation, lifecycle_status
):
    service, store, state = _service_and_state()
    state["connection"]["connection_config"]["oauth"]["lifecycle_status"] = lifecycle_status
    viewer_user = {
        "id": 17,
        "username": "viewer",
        "display_name": "Viewer",
        "is_active": True,
        "roles": ["viewer"],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    client = _authenticated_http_client(store=store, authenticated_user=viewer_user)

    with patch(
        "core.application.management.command_services.ProjectConnectionService",
        return_value=service,
    ):
        path = f"/api/projects/{PROJECT_ID}/connections"
        if read_operation == "get":
            path = f"{path}/{CONNECTION_ID}"
        response = client.get(path, headers={"Authorization": "Bearer viewer-token"})

    assert response.status_code == 200
    payload = response.json()
    connection = payload["connections"][0] if read_operation == "list" else payload
    assert connection == {
        "connection_id": CONNECTION_ID,
        "project_id": PROJECT_ID,
        "extension_id": "acme.notifier",
        "display_name": "Viewer guard",
        "connection_kind": "mcp_connector",
        "connection_type": "api_key",
        "environment": "test",
        "extension_install_id": "",
        "health_status": "",
        "runtime_status": "",
        "last_seen_ok_at": None,
        "enabled": False,
        "oauth_configuration_id": "oauth_config_1",
        "oauth_provider_id": "provider_1",
        "connection_config": {
            "oauth": {
                "lifecycle_status": lifecycle_status,
                "last_error_code": "provider_unavailable",
                "authorization_intent_id": "manager-only-intent",
            }
        },
    }
    assert "manager-only-reconnect-reason" not in response.text
    assert state["connection"]["credential_revision"] == 7

    state["connection"]["connection_config"] = {
        "oauth": {
            "lifecycle_status": "private_lifecycle",
            "last_error_code": "private_provider_error",
        }
    }
    with patch(
        "core.application.management.command_services.ProjectConnectionService",
        return_value=service,
    ):
        rejected = client.get(path, headers={"Authorization": "Bearer viewer-token"})
    rejected_payload = rejected.json()
    rejected_connection = (
        rejected_payload["connections"][0] if read_operation == "list" else rejected_payload
    )
    assert "connection_config" not in rejected_connection
    assert "private_lifecycle" not in rejected.text
    assert "private_provider_error" not in rejected.text


def test_authenticated_operator_http_request_keeps_manageable_connection_fields() -> None:
    service, store, _state = _service_and_state()
    operator_user = {
        "id": 18,
        "username": "operator",
        "display_name": "Operator",
        "is_active": True,
        "roles": ["operator"],
        "account_id": DEFAULT_ACCOUNT_ID,
    }
    client = _authenticated_http_client(store=store, authenticated_user=operator_user)

    with patch(
        "core.application.management.command_services.ProjectConnectionService",
        return_value=service,
    ):
        response = client.get(
            f"/api/projects/{PROJECT_ID}/connections/{CONNECTION_ID}",
            headers={"Authorization": "Bearer viewer-token"},
        )

    assert response.status_code == 200
    connection = response.json()
    assert connection["connection_config"] == {
        "oauth": {
            "lifecycle_status": "needs_attention",
            "last_error_code": "provider_unavailable",
            "authorization_intent_id": "  manager-only-intent  ",
            "connected_identity": {"email": "private@example.test"},
            "provider_detail": "manager-only-provider-detail",
            "reconnect_reason": "manager-only-reconnect-reason",
        },
        "arbitrary": "manager-only",
    }
    assert connection["secret_refs"] == {
        "refs": {"existing": "project_connection:conn_viewer_guard:existing"}
    }
    assert connection["connection_routes"] == [{"route": "manager-only"}]
    assert connection["ingress_config"] == {"path": "/manager-only"}
    assert connection["workflow_mappings"] == {"workflow": "manager-only"}
    assert "secrets" not in connection
    assert "must-not-expose" not in response.text
