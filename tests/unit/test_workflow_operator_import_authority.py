"""Importing over HTTP checks the request's own caller, as a draft save does (A34)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.errors import ServiceForbiddenError
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.entrypoints.api.route_bindings import request_admin_authority
from core.entrypoints.api.workflow_operator import create_workflow_operator_router


def _user(*, roles=("admin",), source="pat", scopes=("admin:all",)) -> dict:
    return {
        "id": "importer",
        "username": "importer",
        "account_id": DEFAULT_ACCOUNT_ID,
        "roles": list(roles),
        "auth_source": source,
        "scopes": list(scopes),
        "principal_type": "user",
    }


def _authority(user: dict):
    def require_user(_request, *, required_roles=None):
        if required_roles and not set(required_roles) & set(user["roles"]):
            raise PermissionError("Administrator required")
        return user

    return request_admin_authority(
        SimpleNamespace(), require_authenticated_user=require_user, auth_admin_roles={"admin"}
    )


def test_an_administrator_token_with_admin_all_may_import() -> None:
    _authority(_user())(None, "Importing a workflow")
    # An interactive administrator session carries no token scopes to check.
    _authority(_user(source="session", scopes=()))(None, "Importing a workflow")


@pytest.mark.parametrize(
    ("user", "message"),
    [
        (_user(scopes=("workflows.write",)), "Importing a workflow requires admin:all"),
        (_user(roles=("operator",)), "Importing a workflow requires an administrator"),
    ],
)
def test_a_narrower_caller_may_not_import(user, message) -> None:
    authority = _authority(user)
    with pytest.raises(ServiceForbiddenError, match=message):
        authority(None, "Importing a workflow")


def test_the_operator_uses_the_request_check_and_otherwise_only_the_cli_admin_scope() -> None:
    calls = []
    http = WorkflowOperatorService(
        get_logger=lambda: None,
        project_query_service=None,
        authoring_authorizer=lambda scope, action: calls.append((scope, action)),
    )
    token_scope = SimpleNamespace(principal_type="user")
    http._require_authoring_admin(token_scope, "Importing a workflow")
    assert calls == [(token_scope, "Importing a workflow")]

    cli = WorkflowOperatorService(get_logger=lambda: None, project_query_service=None)
    cli._require_authoring_admin(SimpleNamespace(principal_type="admin"), "Importing a workflow")
    with pytest.raises(ServiceForbiddenError):
        cli._require_authoring_admin(token_scope, "Importing a workflow")


def test_the_import_route_builds_the_service_for_its_own_request() -> None:
    seen: list[object] = []

    def factory(*args):
        seen.extend(args)
        return SimpleNamespace(import_workflow=lambda **_kwargs: {"imported": ["wf"]})

    app = FastAPI()
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=factory,
            request_scope=lambda request, **kwargs: SimpleNamespace(**kwargs),
            service_error_response=lambda exc, **_kw: JSONResponse({}, status_code=500),
            api_error_response=lambda exc, **_kw: JSONResponse({}, status_code=500),
        )
    )
    response = TestClient(app).post(
        "/api/projects/proj-1/workflow-imports", json={"bundle_yaml": "schema_version: 1\n"}
    )

    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert isinstance(seen[0], Request)
