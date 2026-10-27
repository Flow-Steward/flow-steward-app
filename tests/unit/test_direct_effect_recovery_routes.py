"""The direct recovery routes expose a closed scoped DTO and existing auth transport."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from core.application.management.scope import ScopeContext
from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router


@pytest.fixture
def direct_routes():
    account, project, install, effect = (str(uuid4()) for _ in range(4))
    calls = []
    scope = ScopeContext(
        account_id=account, project_id=project, principal_type="operator", actor_id="operator"
    )
    receipt = {
        "resolution_ref": {
            "command_id": str(uuid4()),
            "effect_id": effect,
            "fenced_version": 2,
            "approval_id": "",
        },
        "action": "reject",
        "observed_version": 1,
        "fenced_version": 2,
    }

    def listing(install_id, *, project_id, scope):
        calls.append(("list", install_id, project_id, scope))
        return SimpleNamespace(dump=lambda: {"effects": [], "truncated": False})

    def resolve(install_id, effect_id, body, *, project_id, scope):
        calls.append(("resolve", install_id, effect_id, body.model_dump(), project_id, scope))
        return SimpleNamespace(dump=lambda: receipt)

    service = SimpleNamespace(list_external_effects=listing, resolve_external_effect=resolve)
    app = FastAPI()
    app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: None,
            event_command_service=lambda: None,
            job_query_service=lambda: None,
            extension_query_service=lambda: None,
            extension_binding_service=lambda: service,
            tool_execution_service=lambda: None,
            command_dispatch_service=lambda: None,
            request_scope=lambda _request, **_kwargs: scope,
            service_error_response=lambda error, *_args: JSONResponse(
                {"error": str(error)}, status_code=error.status_code
            ),
            api_error_response=lambda _error, **_kwargs: JSONResponse(
                {"error": "Unexpected error"}, status_code=500
            ),
        )
    )
    return SimpleNamespace(
        client=TestClient(app),
        calls=calls,
        scope=scope,
        receipt=receipt,
        url=f"/api/projects/{project}/extensions/{install}/external-effects",
        project=project,
        install=install,
        effect=effect,
    )


def test_scoped_summary_reuses_the_existing_extension_service(direct_routes):
    case = direct_routes
    response = case.client.get(case.url)
    assert response.status_code == 200
    assert response.json() == {"effects": [], "truncated": False}
    assert case.calls == [("list", case.install, case.project, case.scope)]


def test_resolution_accepts_only_the_version_action_and_trimmed_comment(direct_routes):
    case = direct_routes
    response = case.client.post(
        f"{case.url}/{case.effect}/resolution",
        json={
            "observed_version": 1,
            "action": "reject",
            "comment": "  Verified no record exists  ",
        },
    )
    assert response.status_code == 200
    assert response.json() == case.receipt
    assert case.calls == [
        (
            "resolve",
            case.install,
            case.effect,
            {"observed_version": 1, "action": "reject", "comment": "Verified no record exists"},
            case.project,
            case.scope,
        )
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"observed_version": True},
        {"observed_version": 0},
        {"observed_version": "1"},
        {"action": "later"},
        {"comment": "  \t"},
        {"claim_token": "forged"},
        {"actor_id": "forged"},
        {"request_fingerprint": "forged"},
        {"resolution_ref": {}},
    ],
)
def test_resolution_rejects_invalid_or_caller_authored_host_facts(direct_routes, change):
    case = direct_routes
    response = case.client.post(
        f"{case.url}/{case.effect}/resolution",
        json={"observed_version": 1, "action": "approve", "comment": "Verified", **change},
    )
    assert response.status_code == 422
    assert case.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"fenced_version": True},
        {"fenced_version": 0},
        {"approval_id": "workflow-approval"},
        {"claim_token": "forged"},
        {"effect_id": "not-a-ledger-uuid"},
    ],
)
def test_direct_action_resolution_reference_is_closed_and_cannot_carry_workflow_authority(change):
    from pydantic import ValidationError

    from core.application.management.requests import ExtensionActionRequest

    ref = {
        "command_id": str(uuid4()),
        "effect_id": str(uuid4()),
        "fenced_version": 2,
        "approval_id": "",
    }
    with pytest.raises(ValidationError):
        ExtensionActionRequest.model_validate({"resolution_ref": {**ref, **change}})
