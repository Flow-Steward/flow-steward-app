"""Installation administrator versus account administrator (multi-account C1).

An administrator acting in the installation account may change installation-wide
resources; an administrator of any other account is refused at each of them and
keeps the admin role inside that account.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    INSTALLATION_ADMIN_REQUIRED_CODE,
    acting_account_id,
    acts_in_installation_account,
    is_installation_account,
    is_installation_admin,
)
from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.scope import ScopeContext

OTHER_ACCOUNT = "acme.second-account"

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


def _scope(account_id: str, principal_type: str = "admin", **kwargs: Any) -> ScopeContext:
    return ScopeContext(
        account_id=account_id, principal_type=principal_type, actor_id="tester", **kwargs
    )


# --- the predicate ---------------------------------------------------------------------


def test_installation_account_is_the_default_account() -> None:
    assert INSTALLATION_ACCOUNT_ID == DEFAULT_ACCOUNT_ID
    assert is_installation_account(DEFAULT_ACCOUNT_ID)
    assert not is_installation_account(OTHER_ACCOUNT)


@pytest.mark.parametrize("empty", ["", "  ", None])
def test_no_account_is_never_the_installation_account(empty: Any) -> None:
    """Fail closed (ADR 0083): an empty account was once read as the installation's."""
    assert not is_installation_account(empty)
    assert acting_account_id({"roles": ["admin"], "account_id": empty}) == ""
    assert not is_installation_admin({"roles": ["admin"], "account_id": empty})
    assert not is_installation_admin({"roles": ["admin"]})


def test_acting_account_reads_mappings_and_scopes() -> None:
    assert acting_account_id(ACCOUNT_ADMIN) == OTHER_ACCOUNT
    assert acting_account_id(_scope(OTHER_ACCOUNT)) == OTHER_ACCOUNT
    assert acting_account_id({"roles": ["admin"]}) == ""
    # Core's own system actor, never a person, is the one documented default.
    assert acting_account_id({"principal_type": "system_actor"}) == DEFAULT_ACCOUNT_ID
    assert not acts_in_installation_account(None)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (INSTALLATION_ADMIN, True),
        (ACCOUNT_ADMIN, False),
        ({**INSTALLATION_ADMIN, "roles": ["operator"]}, False),
        ({"roles": [], "principal_type": "system_actor"}, True),
        (_scope(DEFAULT_ACCOUNT_ID), True),
        (_scope(OTHER_ACCOUNT), False),
        (_scope(DEFAULT_ACCOUNT_ID, "operator"), False),
        (_scope(DEFAULT_ACCOUNT_ID, token_scopes=frozenset({"admin:all"})), True),
        (_scope(DEFAULT_ACCOUNT_ID, token_scopes=frozenset({"projects:write"})), False),
        (_scope(OTHER_ACCOUNT, token_scopes=frozenset({"admin:all"})), False),
        (None, False),
    ],
)
def test_is_installation_admin(principal: Any, expected: bool) -> None:
    assert is_installation_admin(principal) is expected


# --- system email ----------------------------------------------------------------------


def test_system_email_sender_refuses_an_account_admin() -> None:
    from core.application.extensions import system_email_sender

    assert system_email_sender.is_platform_admin(_scope(DEFAULT_ACCOUNT_ID))
    assert not system_email_sender.is_platform_admin(_scope(OTHER_ACCOUNT))
    with pytest.raises(ServiceForbiddenError):
        system_email_sender.select_sender(MagicMock(), _scope(OTHER_ACCOUNT), "")


def _system_email_client(scope: ScopeContext, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from core.application.extensions import system_email_sender
    from core.entrypoints.api.system_email_settings import (
        create_api_system_email_settings_router,
    )

    monkeypatch.setattr(system_email_sender, "settings_view", lambda _bl: {"sender": None})
    app = FastAPI()
    app.include_router(
        create_api_system_email_settings_router(
            get_logger_only=lambda: MagicMock(),
            request_scope=lambda _request: scope,
            service_error_response=lambda exc: JSONResponse(
                dict(exc.payload or {}), status_code=exc.status_code
            ),
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_system_email_settings_route_refuses_an_account_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = _system_email_client(_scope(OTHER_ACCOUNT), monkeypatch).get(
        "/api/settings/system-email"
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "platform_admin_required"

    allowed = _system_email_client(_scope(DEFAULT_ACCOUNT_ID), monkeypatch).get(
        "/api/settings/system-email"
    )
    assert allowed.status_code == 200


# --- extension lifecycle and the global registry ------------------------------------------


def test_extension_lifecycle_fences_refuse_an_account_admin() -> None:
    from core.application.extensions.lifecycle_ports import (
        require_global_extension_lifecycle_scope,
        require_installation_extension_scope,
    )
    from core.application.management.command_services_common import (
        _require_global_extension_lifecycle_scope,
    )

    for fence in (
        require_global_extension_lifecycle_scope,
        _require_global_extension_lifecycle_scope,
        require_installation_extension_scope,
    ):
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            fence(_scope(OTHER_ACCOUNT))
        fence(_scope(DEFAULT_ACCOUNT_ID))


def test_extension_scan_refuses_an_account_admin() -> None:
    from core.application.extensions.binding_service import ExtensionBindingService

    service = ExtensionBindingService.__new__(ExtensionBindingService)
    service._state_service = MagicMock()
    with pytest.raises(ServiceValidationError, match="deployment-scoped"):
        service.scan_extensions(scope=_scope(OTHER_ACCOUNT), _bl=MagicMock())
    service._state_service.scan_extensions.assert_not_called()


def test_trash_lists_every_accounts_own_rows_after_the_fence_was_lifted() -> None:
    # ADR 0020, C5: the default-account-only registry fence is gone; the trash
    # listing keeps each account's own rows, and unowned platform rows are the
    # installation account's.
    from core.application.management.trash_account_scope import trash_account_predicate

    other, other_params = trash_account_predicate(
        has_account_id=True,
        has_project_id=False,
        has_workspace_id=False,
        account_id=OTHER_ACCOUNT,
        installation_account=False,
    )
    assert (other, other_params) == ("account_id = %s", [OTHER_ACCOUNT])
    installation, _ = trash_account_predicate(
        has_account_id=True,
        has_project_id=False,
        has_workspace_id=False,
        account_id=DEFAULT_ACCOUNT_ID,
        installation_account=True,
    )
    assert installation == "(account_id = %s OR account_id IS NULL)"
    unowned, _ = trash_account_predicate(
        has_account_id=False,
        has_project_id=False,
        has_workspace_id=False,
        account_id=OTHER_ACCOUNT,
        installation_account=False,
    )
    assert unowned == "FALSE"


# --- accounts ----------------------------------------------------------------------------


class _AccountsBL:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.disabled: list[str] = []

    def list_accounts(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"account_id": DEFAULT_ACCOUNT_ID, "display_name": "Default", "is_active": True}]

    def create_account(self, *, account_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.created.append(account_id)
        return {"account_id": account_id, "display_name": account_id, "is_active": True}

    def set_account_active(self, account_id: str, *, is_active: bool) -> dict[str, Any]:
        self.disabled.append(account_id)
        return {"account_id": account_id, "is_active": is_active}

    def account_lifecycle_transaction(self) -> AbstractContextManager[None]:
        return nullcontext()


class _Limit:
    def __init__(self, value: int | None) -> None:
        self.value = value

    def limit(self, _account_id: str, _name: str) -> int | None:
        return self.value


def accounts_client(
    bl: Any, auth_user: dict[str, Any], *, entitlement_service: Any = None
) -> TestClient:
    from core.entrypoints.api.auth_tokens_accounts import create_auth_tokens_accounts_router

    def _require_auth(_request: Request, **_kwargs: Any) -> dict[str, Any]:
        return dict(auth_user)

    app = FastAPI()
    app.include_router(
        create_auth_tokens_accounts_router(
            local_auth_enabled=lambda: True,
            get_logger_only=lambda: bl,
            auth_tables_available=lambda _bl: True,
            auth_pat_tables_available=lambda _bl: True,
            auth_account_tables_available=lambda _bl: True,
            auth_service_account_tables_available=lambda _bl: True,
            auth_machine_token_tables_available=lambda _bl: True,
            require_authenticated_user=_require_auth,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            log_auth_audit_event=lambda *_args, **_kwargs: None,
            auth_admin_roles={"admin"},
            err_local_auth_disabled="local-auth-disabled",
            err_database_not_configured="db-missing",
            err_auth_tables_not_available="auth-tables-missing",
            err_pat_tables_not_available="pat-tables-missing",
            err_authentication_required="auth-required",
            err_token_not_found="token-not-found",
            err_account_tables_not_available="account-tables-missing",
            err_admin_role_required="admin-required",
            err_service_account_key_required="service-account-key-required",
            err_machine_token_tables_not_available="machine-token-tables-missing",
            entitlement_service=lambda: entitlement_service,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_account_routes_refuse_an_account_admin() -> None:
    bl = _AccountsBL()
    client = accounts_client(bl, ACCOUNT_ADMIN, entitlement_service=_Limit(10))

    for response in (
        client.get("/api/auth/accounts"),
        client.post("/api/auth/accounts", json={"account_id": "acme.third"}),
        client.post("/api/auth/accounts/acme.other/disable", json={"reason": "x"}),
    ):
        assert response.status_code == 403
        assert response.json()["code"] == INSTALLATION_ADMIN_REQUIRED_CODE
    assert bl.created == []
    assert bl.disabled == []


def test_account_admin_keeps_its_own_account_routes() -> None:
    bl = _AccountsBL()
    bl.get_account = lambda account_id: {"account_id": account_id, "is_active": True}
    bl.update_account = lambda account_id, *, display_name: {
        "account_id": account_id,
        "display_name": display_name,
    }
    client = accounts_client(bl, ACCOUNT_ADMIN)

    assert client.get("/api/auth/accounts/current").status_code == 200
    renamed = client.patch("/api/auth/accounts/current", json={"display_name": "Acme"})
    assert renamed.status_code == 200
    assert renamed.json()["account"]["account_id"] == OTHER_ACCOUNT


def test_account_routes_allow_the_installation_admin() -> None:
    bl = _AccountsBL()
    client = accounts_client(bl, INSTALLATION_ADMIN, entitlement_service=_Limit(10))

    assert client.get("/api/auth/accounts").status_code == 200
    assert client.post("/api/auth/accounts", json={"account_id": "acme.third"}).status_code == 200
    assert (
        client.post("/api/auth/accounts/acme.third/disable", json={"reason": "x"}).status_code
        == 200
    )
    assert bl.created == ["acme.third"]
    assert bl.disabled == ["acme.third"]


# --- core updates, monitoring --------------------------------------------------------------


def test_core_updates_refuse_an_account_admin() -> None:
    from core.application.release.update_notice import CoreUpdateStatus
    from core.entrypoints.api.system_core_updates import create_system_core_updates_router

    def client_for(user: dict[str, Any]) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_system_core_updates_router(
                require_authenticated_user=lambda _request, **_kwargs: dict(user),
                status_provider=lambda: CoreUpdateStatus(status="current", current_version="1.0.0"),
            )
        )
        return TestClient(app, raise_server_exceptions=False)

    refused = client_for(ACCOUNT_ADMIN).get("/api/system/core-updates")
    assert refused.status_code == 403
    assert refused.json()["code"] == INSTALLATION_ADMIN_REQUIRED_CODE
    assert client_for(INSTALLATION_ADMIN).get("/api/system/core-updates").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/system/monitoring/summary"),
        ("GET", "/api/system/monitoring/storage"),
        ("POST", "/api/system/monitoring/storage/orphans/cleanup"),
        ("GET", "/api/system/diagnostics/report"),
    ],
)
def test_monitoring_refuses_an_account_admin(method: str, path: str) -> None:
    from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router

    app = FastAPI()
    app.include_router(
        create_api_system_monitoring_router(
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            require_authenticated_user=lambda _request, **_kwargs: dict(ACCOUNT_ADMIN),
            auth_admin_roles={"admin"},
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    body = {"backend": "", "scope": "", "confirm": ""} if method == "POST" else None
    response = client.request(method, path, json=body)
    assert response.status_code == 403
    assert response.json()["code"] == INSTALLATION_ADMIN_REQUIRED_CODE


# --- worker processes and the watchdog ------------------------------------------------------


def _with_auth_user(app: FastAPI, auth_user: dict[str, Any]) -> TestClient:
    @app.middleware("http")
    async def _inject_auth_user(request: Request, call_next):
        request.state.auth_user = dict(auth_user)
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def test_process_kill_refuses_an_account_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.entrypoints.api import processes as process_routes

    monkeypatch.setattr(process_routes, "terminate_process", lambda pid: {"ok": True, "pid": pid})

    def client_for(user: dict[str, Any]) -> TestClient:
        app = FastAPI()
        app.include_router(process_routes.router)
        return _with_auth_user(app, user)

    refused = client_for(ACCOUNT_ADMIN).post("/api/processes/123/kill")
    assert refused.status_code == 403
    assert "installation administrator" in refused.json()["detail"]
    assert client_for(INSTALLATION_ADMIN).post("/api/processes/123/kill").status_code == 200


def test_watchdog_apply_refuses_an_account_admin() -> None:
    from core.entrypoints.api.jobs import create_api_jobs_router

    def client_for(user: dict[str, Any], scan: MagicMock) -> TestClient:
        query = MagicMock()
        app = FastAPI()
        app.include_router(
            create_api_jobs_router(
                job_query_service=lambda: query,
                job_command_service=MagicMock(),
                approval_query_service=MagicMock(),
                request_scope=lambda _request, **_kwargs: _scope(DEFAULT_ACCOUNT_ID),
                resolve_user_role=MagicMock(return_value="admin"),
                resolve_actor_username=MagicMock(return_value="admin"),
                optional_actor_username=MagicMock(return_value="admin"),
                service_error_response=MagicMock(),
                api_error_response=lambda exc, **_kwargs: JSONResponse(
                    {"error": str(exc)}, status_code=500
                ),
                get_logger_only=lambda: MagicMock(),
                watchdog_scan=scan,
                parse_worker_pid=MagicMock(),
                terminate_worker_pid=MagicMock(),
                append_runtime_journal=MagicMock(),
                build_rerun_workflow_selection=MagicMock(),
                load_replay_snapshot=MagicMock(),
                enqueue_task=MagicMock(),
                retryable_interrupted_step_status="interrupted",
                err_database_not_configured="db-missing",
            )
        )
        return _with_auth_user(app, user)

    scan = MagicMock(return_value={"detection_count": 0, "applied_count": 0, "detections": []})
    refused = client_for(ACCOUNT_ADMIN, scan).post("/api/watchdog/jobs/scan")
    assert refused.status_code == 403
    scan.assert_not_called()

    client_for(INSTALLATION_ADMIN, scan).post("/api/watchdog/jobs/scan")
    scan.assert_called_once()


# --- the global provider catalogue ----------------------------------------------------------


def _providers_client(auth_user: dict[str, Any], bl: MagicMock) -> TestClient:
    from core.entrypoints.api.project_integrations import create_project_integrations_router

    app = FastAPI()
    app.include_router(
        create_project_integrations_router(
            get_logger_only=lambda: bl,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            request_scope=lambda request, project_id="": MagicMock(project_id=project_id),
            require_authenticated_user=lambda *_args, **_kwargs: dict(auth_user),
            to_json_obj=lambda value: dict(value) if isinstance(value, dict) else value,
            api_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
            service_error_response=lambda exc: JSONResponse({"error": str(exc)}, status_code=400),
            secret_storage_config_error=lambda _exc: False,
            auth_admin_roles={"admin"},
            err_database_not_configured="database not configured",
            err_project_not_found="project not found",
            err_admin_role_required="admin required",
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_global_providers_refuse_an_account_admin() -> None:
    bl = MagicMock()
    bl.get_provider.return_value = {"provider_key": "acme.mailer"}
    bl.save_provider_secret.return_value = True
    provider = {"provider_key": "acme.mailer", "provider_type_key": "notification_provider"}
    secret = {"secret_key": "token", "secret_value": "s3cret"}  # pragma: allowlist secret

    client = _providers_client(ACCOUNT_ADMIN, bl)
    refused_provider = client.post("/api/providers", json=provider)
    refused_secret = client.post("/api/providers/acme.mailer/secrets", json=secret)
    assert refused_provider.status_code == 403
    assert refused_provider.json()["code"] == INSTALLATION_ADMIN_REQUIRED_CODE
    assert refused_secret.status_code == 403
    bl.save_provider.assert_not_called()
    bl.save_provider_secret.assert_not_called()

    client = _providers_client(INSTALLATION_ADMIN, bl)
    assert client.post("/api/providers", json=provider).status_code == 200
    assert client.post("/api/providers/acme.mailer/secrets", json=secret).status_code == 200
