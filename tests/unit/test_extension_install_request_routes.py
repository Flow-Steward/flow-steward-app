"""The install request routes (ADR 0084): permissions, transport and composition.

The routes are controllers: they pass the request's scope to the service and turn its
refusals into the platform's error responses. The route table lets only holders of
``extensions.manage`` (administrators) reach them, and a token needs ``admin:all``;
the service then decides account administrator versus installation administrator.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.permissions import ALL_PERMISSION_KEYS, SYSTEM_ROLE_PERMISSIONS
from core.application.auth.route_permissions import (
    route_admin_only,
    route_allowed,
    route_permission,
)
from core.application.extensions.account_enablement import InMemoryExtensionAccountEnablementStore
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.install_requests import (
    InMemoryExtensionInstallRequestStore,
    build_install_request_service,
)
from core.application.management.errors import ServiceError
from core.application.management.scope import ScopeContext
from core.entrypoints.api.extension_install_requests import (
    create_extension_install_requests_router,
)
from core.entrypoints.api.extension_installer import is_installer_route

ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
EXTENSION = "acme.crm-sync"
ROUTES = (
    ("GET", "/api/extensions/install-requests"),
    ("POST", "/api/extensions/install-requests"),
    ("POST", "/api/extensions/install-requests/r-1/cancel"),
    ("POST", "/api/extensions/install-requests/r-1/approve"),
    ("POST", "/api/extensions/install-requests/r-1/decline"),
)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_only_administrators_reach_the_routes(method: str, path: str) -> None:
    match = route_permission(method, path)

    assert match is not None and match.permission == "extensions.manage"
    assert route_admin_only(method, path) is True
    assert route_allowed(method, path, ALL_PERMISSION_KEYS)
    assert not route_allowed(method, path, SYSTEM_ROLE_PERMISSIONS["operator"])
    assert not route_allowed(method, path, SYSTEM_ROLE_PERMISSIONS["viewer"])


def test_the_list_is_not_read_as_an_extension_id() -> None:
    match = route_permission("GET", "/api/extensions/install-requests")

    assert match is not None and match.permission != "extensions.read"


def test_the_routes_stay_in_the_api_process() -> None:
    """Nothing here writes extension files, so nothing goes to the installer process."""
    for method, path in ROUTES:
        assert not is_installer_route(method, path)


class _Logger:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def log_action(self, **values: Any) -> None:
        self.actions.append(values)


@pytest.fixture
def client() -> tuple[TestClient, dict[str, Any]]:
    installs = ExtensionInstallStore()
    installs.account_enablements = InMemoryExtensionAccountEnablementStore()
    requests = InMemoryExtensionInstallRequestStore()
    audit = _Logger()
    state: dict[str, Any] = {"scope": None, "installs": installs, "audit": audit}

    def _service():
        return build_install_request_service(
            get_logger=lambda: audit,
            get_install_store=lambda: installs,
            catalog_name={EXTENSION: "CRM sync"}.get,
            get_store=lambda: requests,
        )

    def _error(exc: ServiceError, request: Request | None = None) -> JSONResponse:
        del request
        return JSONResponse(exc.payload, status_code=exc.status_code)

    def _api_error(exc: Exception, request: Request | None = None) -> JSONResponse:
        del request
        return JSONResponse({"error": str(exc)}, status_code=500)

    app = FastAPI()
    app.include_router(
        create_extension_install_requests_router(
            install_request_service=_service,
            request_scope=lambda _request: state["scope"],
            service_error_response=_error,
            api_error_response=_api_error,
        )
    )
    return TestClient(app), state


def _scope(account_id: str, role: str = "admin") -> ScopeContext:
    return ScopeContext(account_id=account_id, principal_type=role, actor_id="ana")


def test_the_whole_cycle_through_the_routes(client) -> None:
    http, state = client
    state["scope"] = _scope(ACCOUNT_A)

    created = http.post(
        "/api/extensions/install-requests", json={"extension_id": EXTENSION, "note": "please"}
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]
    assert http.get("/api/extensions/install-requests").json()["items"][0]["status"] == "pending"

    state["scope"] = _scope(INSTALLATION_ACCOUNT_ID)
    refused = http.post(f"/api/extensions/install-requests/{request_id}/approve")
    assert refused.status_code == 409
    assert refused.json()["code"] == "extension_not_installed"
    state["installs"].install(
        extension_id=EXTENSION, version="1.0.0", account_id=INSTALLATION_ACCOUNT_ID
    )
    approved = http.post(f"/api/extensions/install-requests/{request_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert (
        state["installs"]
        .account_enablements.get(account_id=ACCOUNT_A, extension_id=EXTENSION)
        .enabled
    )
    assert [row["action_type"] for row in state["audit"].actions] == [
        "extension_install_request_created",
        "extension_install_request_approved",
    ]


def test_cancel_and_decline_through_the_routes(client) -> None:
    http, state = client
    state["scope"] = _scope(ACCOUNT_A)
    first = http.post("/api/extensions/install-requests", json={"extension_id": EXTENSION}).json()
    cancelled = http.post(f"/api/extensions/install-requests/{first['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    second = http.post("/api/extensions/install-requests", json={"extension_id": EXTENSION}).json()

    state["scope"] = _scope(INSTALLATION_ACCOUNT_ID)
    declined = http.post(
        f"/api/extensions/install-requests/{second['id']}/decline", json={"reason": "Not now"}
    )
    assert declined.json()["decline_reason"] == "Not now"
    without_body = http.post(f"/api/extensions/install-requests/{second['id']}/decline")
    assert without_body.status_code == 409


def test_refusals_keep_their_status(client) -> None:
    http, state = client
    state["scope"] = _scope(ACCOUNT_A, "operator")
    assert http.get("/api/extensions/install-requests").status_code == 403
    state["scope"] = _scope(ACCOUNT_A)
    assert (
        http.post(
            "/api/extensions/install-requests", json={"extension_id": EXTENSION, "x": 1}
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/api/extensions/install-requests",
            json={"extension_id": EXTENSION, "note": "x" * 501},
        ).status_code
        == 422
    )
    request_id = http.post(
        "/api/extensions/install-requests", json={"extension_id": EXTENSION}
    ).json()["id"]
    assert http.post(f"/api/extensions/install-requests/{request_id}/approve").status_code == 403
