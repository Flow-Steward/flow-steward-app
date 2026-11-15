"""Authentication storage reads must not serialize unrelated HTTP requests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request

from core.application.auth.auth_http_runtime import local_auth_middleware
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect
from tests.support.api_read_probe import BlockedRead, probe_while_read_waits


def _auth_app(reader, boundary):
    app = FastAPI()
    external = boundary == "external"
    principal = ResolvedPrincipal(
        id="reader", roles=["admin"], auth_source="external" if external else "local"
    )
    user = None if boundary == "bootstrap" else principal

    def value(name, result):
        return reader.returning(result) if boundary == name else lambda *_args, **_kwargs: result

    def active(_account_id):
        raise RuntimeError("Account unavailable")

    store = SimpleNamespace(is_account_active=value("account", True))
    if boundary == "account_denied":
        store.is_account_active = reader.returning(False)
    elif boundary == "account_error":

        def unavailable(*_args):
            reader.returning(None)()
            return active(*_args)

        store.is_account_active = unavailable

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path == "/test-heartbeat":
            return await call_next(request)
        request.scope["headers"].append((b"cookie", b"session=unit-test-session"))
        return await local_auth_middleware(
            request,
            call_next,
            local_auth_enabled_fn=lambda: not external,
            external_auth_enabled=external,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=value("external", principal),
            normalize_live_principal_fn=lambda raw, **_kwargs: raw,
            get_logger_only_fn=value("logger", store),
            auth_tables_available_fn=value("tables", True),
            get_user_by_session_token_fn=value("token", user),
            local_auth_bootstrap_required_fn=value("bootstrap", True),
            auth_account_tables_available_fn=value("account_tables", True),
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: True,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account disabled",
            err_admin_role_required="Admin required",
            err_operator_role_required="Operator required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )

    @app.get("/api/private")
    async def private(request: Request):
        return {"id": request.state.auth_user["id"], "source": request.state.auth_source}

    return app


@pytest.mark.parametrize(
    "boundary,status",
    [
        ("logger", 200),
        ("external", 200),
        ("tables", 200),
        ("token", 200),
        ("account_tables", 200),
        ("account", 200),
        ("bootstrap", 401),
        ("account_denied", 403),
        ("account_error", 503),
    ],
)
def test_auth_storage_read_does_not_block_other_http_requests(boundary, status):
    reader = BlockedRead()
    heartbeat, response = asyncio.run(
        probe_while_read_waits(_auth_app(reader, boundary), "/api/private", reader)
    )
    assert response.status_code == status, response.text
    assert heartbeat.json() == {"read_finished": False}, f"Auth {boundary} blocked the event loop"
    if status == 200:
        assert response.json() == {
            "id": "reader",
            "source": "external" if boundary == "external" else "local",
        }
