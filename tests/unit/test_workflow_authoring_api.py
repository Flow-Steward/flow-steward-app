"""HTTP authoring contracts with real application validation and creation policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.principal import roles_intersect
from core.application.auth.request_principal_runtime import require_authenticated_user
from core.application.management.scope import scope_from_principal
from core.application.registry.registry_manager import RegistryManager
from core.entrypoints.api.route_bindings import build_workflow_authoring_service
from core.entrypoints.api.workflow_authoring import create_workflow_authoring_router
from core.infrastructure.runtime import serve_request_runtime
from core.infrastructure.runtime.maintenance_http import build_maintenance_http_middleware
from tests.unit.test_workflow_authoring_catalog import tool
from tests.unit.test_workflow_authoring_drafts import DraftDB, bundle

BASE = "/api/projects/project/workflow-authoring"


def _service_error(exc, request=None):
    return serve_request_runtime.service_error_response(
        exc,
        request=request,
        error_payload=serve_request_runtime.build_error_payload_factory(
            err_request_failed="Request failed", default_correlation_id_fn=lambda: "test"
        ),
        request_correlation_id=lambda req, **kwargs: serve_request_runtime.request_correlation_id(
            req, new_id_fn=lambda: "test", **kwargs
        ),
        error_code_for_status=serve_request_runtime.error_code_for_status,
        err_request_failed="Request failed",
    )


def _client(monkeypatch, *, user=None, maintenance=False):
    db = DraftDB()
    db.tools["publish"] = tool("publish")
    refreshed = []
    registry = SimpleNamespace(db_store=db, refresh_workflow_cache=lambda: refreshed.append(True))
    monkeypatch.setattr(RegistryManager, "instance", lambda: registry)
    principal = user or {
        "id": "creator",
        "username": "creator",
        "account_id": DEFAULT_ACCOUNT_ID,
        "roles": ["admin"],
        "auth_source": "pat",
        "scopes": ["admin:all"],
        "principal_type": "user",
    }

    def require_user(request, *, required_roles=None):
        return require_authenticated_user(
            request,
            required_roles=required_roles,
            request_auth_user_payload_fn=lambda req: req.state.auth_user,
            roles_intersect_fn=roles_intersect,
            err_authentication_required="Authentication required",
        )

    def factory(request):
        return build_workflow_authoring_service(
            request=request,
            get_project_store=lambda: db,
            get_integration_store=lambda: db,
            get_extension_install_store=lambda: None,
            require_authenticated_user=require_user,
            auth_admin_roles={"admin"},
        )

    app = FastAPI()
    app.middleware("http")(
        build_maintenance_http_middleware(maintenance_active=lambda: maintenance)
    )

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.auth_user = principal
        return await call_next(request)

    app.include_router(
        create_workflow_authoring_router(
            workflow_authoring_service=factory,
            request_scope=lambda request, **kwargs: scope_from_principal(
                request.state.auth_user, **kwargs
            ),
            service_error_response=_service_error,
            api_error_response=lambda exc, **kwargs: JSONResponse(
                {"error": "Internal server error"}, status_code=500
            ),
        )
    )
    return TestClient(app), db, refreshed


def test_api_search_describe_examples_and_schema_use_live_application_contracts(monkeypatch):
    client, db, refreshed = _client(monkeypatch)
    search = client.get(
        BASE + "/capabilities",
        params=[("query", "publish"), ("kinds", "tool"), ("kinds", "workflow"), ("limit", "1")],
    )
    assert search.status_code == 200
    assert [row["ref"] for row in search.json()["capabilities"]] == ["tool:publish"]
    description = client.get(BASE + "/capability", params={"ref": "tool:publish"})
    assert description.status_code == 200
    assert description.json()["ref"] == "tool:publish"
    examples = client.get(BASE + "/examples", params={"query": "none", "limit": 1})
    assert examples.status_code == 200
    assert examples.json()["examples"] == []
    schema = client.get("/api/workflow-authoring/schema", params={"step_kind": "set_variable"})
    assert schema.status_code == 200
    assert schema.json()["format"] == "platform.workflow_bundle.v2"
    assert [row["step_kind"] for row in schema.json()["step_kinds"]] == ["set_variable"]
    assert db.workflows == {}
    assert refreshed == []


def test_api_validate_and_create_preserve_disabled_result_and_conflict(monkeypatch):
    client, db, refreshed = _client(monkeypatch)
    payload = {"bundle_yaml": bundle(enabled=True), "resolutions": []}
    validated = client.post(BASE + "/validate", json=payload)
    assert validated.status_code == 200
    assert validated.json()["can_save"] is True
    assert db.workflows == {}
    assert refreshed == []
    saved = client.post(BASE + "/drafts", json=payload)
    assert saved.status_code == 200
    assert saved.json()["enabled"] is False
    assert saved.json()["issues"] == validated.json()["issues"]
    assert saved.json()["editor_url"] == "/registry/workflows/new_workflow/builder"
    assert db.workflows["new_workflow"]["enabled"] is False
    assert refreshed == [True]
    conflict = client.post(BASE + "/drafts", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["workflow_id"] == "new_workflow"
    assert len(db.history) == 1
    assert refreshed == [True]


@pytest.mark.parametrize(
    "text,code",
    [
        ("workflows: [\nprivate_marker: sensitive_source_excerpt", "bundle_yaml_invalid"),
        (bundle(settings="invalid_scalar"), "authoring.step_config_shape_invalid"),
    ],
)
def test_api_invalid_draft_returns_safe_structured_422(monkeypatch, text, code):
    client, db, refreshed = _client(monkeypatch)
    response = client.post(BASE + "/drafts", json={"bundle_yaml": text})
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["issues"][0]["path"]
    assert response.json()["issues"][0]["repair_guidance"]
    assert "sensitive_source_excerpt" not in response.text
    assert db.workflows == {}
    assert refreshed == []


@pytest.mark.parametrize(
    "roles,scopes,source,kind,expected",
    [
        (["admin"], ["admin:all"], "pat", "user", 200),
        (["admin"], ["workflows.write"], "pat", "user", 403),
        (["operator"], ["admin:all"], "machine", "service_account", 403),
        (["operator"], ["workflows.write"], "machine", "service_account", 403),
        (["admin"], [], "local", "user", 200),
    ],
)
def test_application_creation_callback_enforces_existing_roles_and_token_scope(
    monkeypatch, roles, scopes, source, kind, expected
):
    client, db, refreshed = _client(
        monkeypatch,
        user={
            "account_id": DEFAULT_ACCOUNT_ID,
            "roles": roles,
            "scopes": scopes,
            "auth_source": source,
            "principal_type": kind,
        },
    )
    response = client.post(BASE + "/drafts", json={"bundle_yaml": bundle()})
    assert response.status_code == expected
    assert bool(db.workflows) is (expected == 200)
    assert refreshed == ([True] if expected == 200 else [])


def test_api_denied_project_cannot_validate_or_save(monkeypatch):
    client, db, refreshed = _client(
        monkeypatch,
        user={
            "account_id": DEFAULT_ACCOUNT_ID,
            "roles": ["admin"],
            "scopes": ["admin:all"],
            "auth_source": "pat",
            "allowed_project_ids": ["other"],
        },
    )
    for action in ("validate", "drafts"):
        response = client.post(BASE + "/" + action, json={"bundle_yaml": bundle()})
        assert response.status_code == 403
    assert db.workflows == {}
    assert refreshed == []


def test_post_validation_still_obeys_existing_http_maintenance_policy(monkeypatch):
    client, db, refreshed = _client(monkeypatch, maintenance=True)
    response = client.post(BASE + "/validate", json={"bundle_yaml": bundle()})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "maintenance_mode_enabled"
    assert db.workflows == {}
    assert refreshed == []


def test_api_authoring_keeps_current_default_account_restriction(monkeypatch):
    client, db, refreshed = _client(
        monkeypatch,
        user={
            "account_id": "other-account",
            "roles": ["admin"],
            "scopes": ["admin:all"],
            "auth_source": "pat",
        },
    )
    response = client.post(BASE + "/validate", json={"bundle_yaml": bundle()})
    assert response.status_code == 403
    assert db.workflows == {}
    assert refreshed == []


def test_safe_draft_error_preserves_forwarded_caller_correlation(monkeypatch):
    client, db, refreshed = _client(monkeypatch)
    response = client.post(
        BASE + "/drafts",
        json={"bundle_yaml": "workflows: ["},
        headers={"X-Correlation-ID": "caller-authoring-repair"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "bundle_yaml_invalid"
    assert response.json()["correlation_id"] == "caller-authoring-repair"
    assert db.workflows == {}
    assert refreshed == []


def test_schema_discovery_is_serialized_without_project_or_mutation(monkeypatch):
    client, db, refreshed = _client(monkeypatch)
    response = client.get("/api/workflow-authoring/schema", params={"step_kind": "set_variable"})
    assert response.status_code == 200
    payload = response.json()
    assert [row["step_kind"] for row in payload["step_kinds"]] == ["set_variable"]
    guidance = payload["definition_guidance"]
    assert guidance["contract_level"] == "partial_guidance_and_examples"
    assert {row["id"] for row in guidance["examples"]} == {
        "group_sequence",
        "group_lanes",
        "for_each_sequence",
        "for_each_lanes",
        "hook_unbound",
    }
    assert {row["id"] for row in guidance["change_set"]["partial_examples"]} == {
        "group_for_each_atomic",
        "root_hook_atomic",
    }
    assert db.workflows == {}
    assert db.history == []
    assert refreshed == []
