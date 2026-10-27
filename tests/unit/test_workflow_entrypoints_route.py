"""Unit tests for workflow entrypoint route response shaping helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope
from core.entrypoints.api.workflow_entrypoints import (
    _entrypoint_success_response,
    _generate_entry_key,
    _normalize_response_mode,
)
from core.infrastructure.db.schema_state import read_canonical_schema_sql
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_expired_application_outcome_maps_to_http_410() -> None:
    from core.entrypoints.api.workflow_entrypoints_routes_execute import (
        _application_failure_http_status,
    )

    assert _application_failure_http_status("gone") == 410


def _workflow_entrypoint_test_surface():
    """Test-only patch router for the split implementation modules.

    Production no longer mirrors globals through a compatibility facade.  The
    older route tests still benefit from one patch handle, so this helper fans
    assignments out only inside the test process.
    """
    from core.application.workflows import workflow_entrypoint_input_service
    from core.entrypoints.api import workflow_entrypoints as public_mod
    from core.entrypoints.api import (
        workflow_entrypoints_common,
        workflow_entrypoints_execution_auth,
        workflow_entrypoints_registry,
        workflow_entrypoints_router,
        workflow_entrypoints_routes_execute,
        workflow_entrypoints_routes_providers,
        workflow_entrypoints_routes_webhooks,
        workflow_entrypoints_serializers,
    )

    modules = (
        public_mod,
        workflow_entrypoints_common,
        workflow_entrypoints_registry,
        workflow_entrypoints_serializers,
        workflow_entrypoints_routes_webhooks,
        workflow_entrypoints_routes_providers,
        workflow_entrypoints_execution_auth,
        workflow_entrypoints_routes_execute,
        workflow_entrypoints_router,
        workflow_entrypoint_input_service,
    )

    class _Surface:
        def __getattr__(self, name: str):
            for module in modules:
                if hasattr(module, name):
                    return getattr(module, name)
            raise AttributeError(name)

        def __setattr__(self, name: str, value: Any) -> None:
            matched = False
            for module in modules:
                if hasattr(module, name):
                    setattr(module, name, value)
                    matched = True
            if not matched:
                raise AttributeError(name)

    return _Surface()


def test_normalize_response_mode_defaults_to_accepted_for_unknown_values():
    assert _normalize_response_mode("") == "accepted"
    assert _normalize_response_mode("unexpected") == "accepted"


def test_entrypoint_success_response_returns_202_for_accepted_mode():
    response = _entrypoint_success_response(
        {"status": "accepted"},
        response_mode="accepted",
    )

    assert response.status_code == 202
    assert response.headers.get("x-workflow-response-mode") == "accepted"
    assert json.loads(response.body.decode("utf-8"))["status"] == "accepted"


def test_entrypoint_success_response_returns_200_for_ok_mode():
    response = _entrypoint_success_response(
        {"status": "accepted", "job_id": 42},
        response_mode="ok",
    )

    assert response.status_code == 200
    assert response.headers.get("x-workflow-response-mode") == "ok"
    body = json.loads(response.body.decode("utf-8"))
    assert body["job_id"] == 42


def test_entrypoint_success_response_returns_204_for_no_content_mode():
    response = _entrypoint_success_response(
        {"status": "accepted"},
        response_mode="no_content",
    )

    assert response.status_code == 204
    assert response.headers.get("x-workflow-response-mode") == "no_content"
    assert response.body == b""


def test_entrypoint_success_response_keeps_dedup_as_200():
    response = _entrypoint_success_response(
        {"status": "accepted", "deduplicated": True},
        response_mode="accepted",
        deduplicated=True,
    )

    assert response.status_code == 200
    assert response.headers.get("x-workflow-response-mode") == "accepted"


def test_duplicate_no_content_records_the_actual_204_status(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        entrypoint_overrides={"response_mode": "no_content"},
    )
    headers = {"Authorization": "Bearer token-123", "Idempotency-Key": "same-key"}

    first = client.post(
        "/webhook/workflows/inventory_sync", json={"title": "same"}, headers=headers
    )
    second = client.post(
        "/webhook/workflows/inventory_sync", json={"title": "same"}, headers=headers
    )

    assert (first.status_code, second.status_code) == (204, 204)
    assert [row["meta"]["http_status"] for row in state["logs"][-2:]] == [204, 204]


def test_workflow_entrypoint_storage_tables_are_in_canonical_schema():
    schema = read_canonical_schema_sql()
    entrypoint_table = schema.split("CREATE TABLE public.workflow_entrypoints (", 1)[1].split(
        ");", 1
    )[0]

    assert "CREATE TABLE public.workflow_entrypoints" in schema
    assert "project_id uuid NOT NULL" in entrypoint_table
    assert "allowed_methods_jsonb" not in entrypoint_table
    assert "CREATE TABLE public.request_idempotency" in schema
    assert "CREATE TABLE public.workflow_provider_triggers" in schema
    provider_table = schema.split("CREATE TABLE public.workflow_provider_triggers (", 1)[1].split(
        ");", 1
    )[0]
    assert "project_id uuid NOT NULL" in provider_table
    assert "workflow_provider_triggers_project_account_fkey" in schema
    assert "workflow_provider_triggers_project_workflow_fkey" in schema
    assert (
        "CREATE UNIQUE INDEX workflow_provider_triggers_trigger_scope_key "
        "ON public.workflow_provider_triggers USING btree "
        "(workflow_id, project_id, provider_key, trigger_type_id, COALESCE(event_name, ''::text))"
    ) in schema or (
        "ADD CONSTRAINT workflow_provider_triggers_trigger_scope_key UNIQUE "
        "(workflow_id, project_id, provider_key, trigger_type_id, COALESCE(event_name, ''::text))"
    ) in schema
    assert "ADD CONSTRAINT workflow_entrypoints_entry_key_key UNIQUE (entry_key)" in schema
    assert (
        "workflow_entrypoints_project_workflow_fkey FOREIGN KEY (project_id, workflow_id) "
        "REFERENCES public.project_workflows(project_id, workflow_id) ON DELETE CASCADE" in schema
    )
    assert "auth_mode text DEFAULT 'bearer_token'::text NOT NULL" in schema
    assert (
        "allowed_content_types_jsonb jsonb DEFAULT '[\"application/json\"]'::jsonb NOT NULL"
        in schema
    )
    assert ("ADD CONSTRAINT request_idempotency_pkey PRIMARY KEY (scope_key, key_digest)") in schema


def test_workflow_entrypoint_request_rejects_typo_and_unbounded_file_limits() -> None:
    from core.entrypoints.api.workflow_entrypoints_serializers import (
        WorkflowEntrypointCreateRequest,
    )

    with pytest.raises(ValueError):
        WorkflowEntrypointCreateRequest.model_validate(
            {"project_id": "project-1", "idempotency_mode": "hearder"}
        )
    with pytest.raises(ValueError):
        WorkflowEntrypointCreateRequest.model_validate(
            {"project_id": "project-1", "max_file_bytes": 104857601}
        )
    with pytest.raises(ValueError):
        WorkflowEntrypointCreateRequest.model_validate({"project_id": "project-1", "max_files": 33})


def test_provider_trigger_uses_selected_project_account(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    client = _build_provider_trigger_client(
        monkeypatch,
        create_impl=lambda _bl, **kwargs: (
            captured.update(kwargs)
            or {
                **kwargs,
                "created_at": "2026-05-11T00:00:00Z",
                "updated_at": "2026-05-11T00:00:00Z",
            }
        ),
    )

    response = client.post(
        "/api/workflows/wf.inventory.sync/triggers/providers",
        json={
            "project_id": "proj-1",
            "provider_key": "sample_tasks",
            "trigger_type_id": "task-created",
            "enabled": True,
            "trigger_config": {"space_id": "space-1"},
        },
    )

    assert response.status_code == 201
    assert captured["account_id"] == DEFAULT_ACCOUNT_ID


def test_trigger_management_routes_keep_explicit_scope_boundary():
    api_dir = Path(__file__).resolve().parents[2] / "core" / "entrypoints" / "api"
    source = "\n".join(
        (api_dir / filename).read_text(encoding="utf-8")
        for filename in (
            "workflow_entrypoints_routes_webhooks.py",
            "workflow_entrypoints_routes_providers.py",
            "workflow_entrypoints_routes_execute.py",
        )
    )

    route_markers = (
        ("post", "/api/workflows/{workflow_id}/triggers/webhooks"),
        ("patch", "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}"),
        ("post", "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/rotate-secret"),
        (
            "post",
            "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/regenerate-entry-key",
        ),
        ("delete", "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}"),
        ("post", "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/dry-run"),
        ("get", "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/deliveries"),
        ("post", "/api/workflows/{workflow_id}/triggers/providers"),
        ("patch", "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}"),
        ("delete", "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}"),
    )

    def route_position(method: str, path: str) -> int:
        match = re.search(rf'@router\.{method}\(\s*"{re.escape(path)}"', source)
        assert match is not None
        return match.start()

    def public_entrypoint_position() -> int:
        match = re.search(r'@router\.post\(\s*"/webhook/workflows/\{entry_key\}"', source)
        assert match is not None
        return match.start()

    marker_positions = [route_position(method, path) for method, path in route_markers]
    public_route_position = public_entrypoint_position()
    blocks = []
    for index, start in enumerate(marker_positions):
        end = (
            marker_positions[index + 1]
            if index + 1 < len(marker_positions)
            else public_route_position
        )
        blocks.append(source[start:end])

    for block in blocks:
        assert "scope = scope_resolver(request)" in block
        assert "_require_workflow_scope(scope, workflow_id)" in block
        assert (
            "_require_project_scope(" in block
            or "_require_existing_entrypoint_project_scope(" in block
        )


def _build_entrypoint_client(
    monkeypatch,
    *,
    idempotency_mode: str = "none",
    claim_impl=None,
    entrypoint_overrides: dict[str, Any] | None = None,
    expected_input_names: set[str] | None = None,
    artifact_plane=None,
    request_scope=None,
    redeliver_task=None,
    enqueue_impl=None,
    validation_impl=None,
    workflow_enabled: bool = True,
    workflow_exists: bool = True,
    workflow_lookup_error: Exception | None = None,
) -> tuple[TestClient, dict[str, Any]]:
    mod = _workflow_entrypoint_test_surface()

    entrypoint = {
        "entry_key": "inventory_sync",
        "workflow_id": "wf.inventory.sync",
        "project_id": "7",
        "enabled": True,
        "auth_mode": "bearer_token",
        "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
        "source_restrictions_jsonb": {},
        "allowed_content_types_jsonb": ["application/json"],
        "max_body_bytes": 4096,
        "max_file_bytes": 4096,
        "max_files": 2,
        "idempotency_mode": idempotency_mode,
        "input_mapping_jsonb": {},
        "secret_ref": "workflow_entrypoint.inventory_sync.secret",
        "response_mode": "accepted",
    }
    entrypoint.update(dict(entrypoint_overrides or {}))
    monkeypatch.setattr(
        "core.application.workflows.workflow_webhook_creation_service.lock_entrypoint",
        lambda *_args, **_kwargs: dict(entrypoint),
    )

    monkeypatch.setattr(mod, "_load_workflow_entrypoint", lambda _bl, _entry_key: dict(entrypoint))
    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-1"},
    )
    monkeypatch.setattr(
        mod,
        "_resolve_project_run_config_for_workflow",
        lambda _bl, _workflow_id: (7, {"project_name": "Storefront", "project_root": "/target"}),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "resolve_project_run_config_for_workflow_project",
        lambda _bl, _workflow_id, project_id: (
            project_id,
            {"project_name": "Storefront", "project_root": "/target"},
        ),
    )
    monkeypatch.setattr(
        mod,
        "_expected_input_names",
        lambda _workflow_id: set(expected_input_names or {"title"}),
    )

    class _WorkflowInterfaceService:
        def interface_for_workflow_id(self, workflow_id):
            return SimpleNamespace(
                expected_inputs=[
                    SimpleNamespace(name=name) for name in sorted(expected_input_names or {"title"})
                ],
                enabled=True,
                workflow_id=workflow_id,
            )

        def validate_inputs(self, interface, payload, channel=""):
            return SimpleNamespace(
                ok=True,
                errors=[],
                inputs=dict(payload or {}),
                interface={"enabled": True, "channel": channel},
            )

        def validate_inputs_for_workflow_id(self, workflow_id, inputs, channel):
            if validation_impl is not None:
                return validation_impl(workflow_id, inputs, channel)
            return type(
                "ValidationResult",
                (),
                {
                    "ok": True,
                    "errors": [],
                    "inputs": dict(inputs or {}),
                    "interface": {"enabled": True, "workflow_id": workflow_id, "channel": channel},
                },
            )()

    monkeypatch.setattr(mod, "WorkflowInterfaceService", _WorkflowInterfaceService)
    monkeypatch.setattr(mod, "_renew_idempotency_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(mod, "_journal_idempotency_reservation", lambda *_args, **_kwargs: True)
    if claim_impl is not None:
        monkeypatch.setattr(mod, "_claim_idempotency_reservation", claim_impl)
    else:
        reservations: dict[tuple[str, str], dict[str, Any]] = {}

        def _claim(_bl, *, entry_key, idempotency_key, request_id, request_fingerprint=None):
            state["reservation_attempts"] += 1
            key = (str(entry_key), str(idempotency_key))
            existing = reservations.get(key)
            if existing:
                return {**existing, "claimed": False}
            row = {
                "entry_key": str(entry_key),
                "idempotency_key": str(idempotency_key),
                "request_id": str(request_id),
                "status": "pending",
                "job_id": None,
            }
            reservations[key] = row
            return {**row, "claimed": True}

        def _mark_accepted(
            _bl, *, entry_key, idempotency_key, request_id, job_id, request_fingerprint=None
        ):
            key = (str(entry_key), str(idempotency_key))
            reservations[key] = {
                "entry_key": str(entry_key),
                "idempotency_key": str(idempotency_key),
                "request_id": str(request_id),
                "status": "accepted",
                "job_id": str(job_id),
            }

        def _mark_failed(
            _bl, *, entry_key, idempotency_key, request_id, error_text, request_fingerprint=None
        ):
            key = (str(entry_key), str(idempotency_key))
            reservations[key] = {
                "entry_key": str(entry_key),
                "idempotency_key": str(idempotency_key),
                "request_id": str(request_id),
                "status": "failed",
                "job_id": None,
                "last_error": str(error_text),
            }

        monkeypatch.setattr(mod, "_claim_idempotency_reservation", _claim)
        monkeypatch.setattr(mod, "_mark_idempotency_reservation_accepted", _mark_accepted)
        monkeypatch.setattr(mod, "_mark_idempotency_reservation_failed", _mark_failed)

    state: dict[str, Any] = {
        "enqueued": 0,
        "logs": [],
        "events": [],
        "last_enqueue": None,
        "reservation_attempts": 0,
        "workflow_reads": [],
    }

    def _enqueue_task(**_kwargs):
        state["enqueued"] = int(state["enqueued"]) + 1
        state["last_enqueue"] = dict(_kwargs)
        if enqueue_impl is not None:
            return enqueue_impl(**_kwargs)
        return 501

    app = FastAPI()

    class _WorkflowCursor:
        """Keep the real owner-state repository query; replace only database I/O."""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, query, params):
            assert "registry_workflows" in str(query)
            assert tuple(params) == ("wf.inventory.sync",)
            state["workflow_reads"].append(tuple(params))
            if workflow_lookup_error is not None:
                raise workflow_lookup_error

        def fetchone(self):
            if not workflow_exists:
                return None
            return {
                "workflow_id": "wf.inventory.sync",
                "enabled": workflow_enabled,
                "deleted_at": None,
            }

    state["workflow_connection"] = SimpleNamespace(cursor=_WorkflowCursor)

    class _BL:
        conn = state["workflow_connection"]

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": "acct-1", "enabled": True}

        def get_project_secret(self, *, project_id, secret_key):
            return "token-123"

        def get_job(self, job_id):
            return {
                "id": str(job_id),
                "status": "in_progress",
                "worker_id": "test-worker",
                "meta": {"queue_delivery": {"dispatch_state": "publisher_confirmed"}},
            }

        def log_action(self, **kwargs):
            state["logs"].append(dict(kwargs))

        def record_domain_event(self, **kwargs):
            state["events"].append(dict(kwargs))
            return dict(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            request_scope=request_scope,
            enqueue_task=_enqueue_task,
            redeliver_task=redeliver_task,
            artifact_plane=artifact_plane,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )
    return TestClient(app), state


def _build_provider_trigger_client(
    monkeypatch,
    *,
    create_impl=None,
    update_impl=None,
    request_scope=None,
) -> TestClient:
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-1"},
    )
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-1"),
            {"project_name": "Demo", "project_root": "/tmp/demo", "workflow_id": workflow_id},
        ),
    )
    monkeypatch.setattr(
        mod,
        "find_provider_trigger_definition",
        lambda _bl, provider_key, trigger_type_id: {
            "provider_key": provider_key,
            "trigger_type_id": trigger_type_id,
            "display_name": "Sample Tasks task created",
            "description": "Run on provider event",
            "ui_schema": {},
            "config_schema": {},
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_provider_trigger_config",
        lambda _definition, _config, *, enabled: (
            "ready" if enabled else "draft",
            [],
            "task.created",
        ),
    )

    monkeypatch.setattr(
        mod.workflow_provider_trigger_repository,
        "create_workflow_provider_trigger",
        create_impl
        or (
            lambda _bl, **kwargs: {
                **kwargs,
                "created_at": "2026-05-11T00:00:00Z",
                "updated_at": "2026-05-11T00:00:00Z",
            }
        ),
    )
    monkeypatch.setattr(
        mod.workflow_provider_trigger_repository,
        "get_workflow_provider_trigger",
        lambda _bl, trigger_id: {
            "trigger_id": trigger_id,
            "workflow_id": "wf.inventory.sync",
            "project_id": "proj-1",
            "provider_key": "sample_tasks",
            "trigger_type_id": "task-created",
            "display_name": "Sample Tasks task created",
            "enabled": True,
            "event_name": "task.created",
            "trigger_config_jsonb": {"space_id": "space-1"},
            "readiness_status": "ready",
            "validation_errors_jsonb": [],
            "created_at": "2026-05-11T00:00:00Z",
            "updated_at": "2026-05-11T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        mod.workflow_provider_trigger_repository,
        "update_workflow_provider_trigger",
        update_impl
        or (
            lambda _bl, trigger_id, **kwargs: {
                "trigger_id": trigger_id,
                "workflow_id": "wf.inventory.sync",
                **kwargs,
                "trigger_config_jsonb": kwargs.get("trigger_config_jsonb", {}),
                "created_at": "2026-05-11T00:00:00Z",
                "updated_at": "2026-05-11T00:00:01Z",
            }
        ),
    )

    app = FastAPI()

    class _BL:
        def get_project(self, project_id):
            return {
                "id": str(project_id),
                "account_id": DEFAULT_ACCOUNT_ID,
                "enabled": True,
            }

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            request_scope=request_scope,
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_provider_trigger_route"),
            err_database_not_configured="database_unavailable",
        )
    )
    return TestClient(app)


def test_workflow_entrypoint_router_registers_only_canonical_public_post_route():
    mod = _workflow_entrypoint_test_surface()

    router = mod.create_workflow_entrypoints_router(
        get_logger_only=lambda: object(),
        enqueue_task=lambda **_kwargs: 1,
        artifact_plane=None,
        logger=logging.getLogger("tests.workflow_entrypoints_route"),
        err_database_not_configured="database_unavailable",
    )
    public_route = next(
        r for r in router.routes if getattr(r, "path", "") == "/webhook/workflows/{entry_key}"
    )
    assert set(public_route.methods or set()) == {"POST"}
    assert public_route.responses[429]["description"] == "Multipart admission capacity exhausted"
    assert public_route.responses[429]["headers"]["Retry-After"]["schema"] == {
        "type": "integer",
        "minimum": 1,
    }
    assert not any(
        getattr(r, "path", "") == "/api/workflow-entrypoints/{entry_key}" for r in router.routes
    )


def test_workflow_entrypoint_missing_entrypoint_records_rejected_ingress_event(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(mod, "_load_workflow_entrypoint", lambda _bl, _entry_key: {})

    response = client.post("/webhook/workflows/missing-key", json={"title": "Sync inventory"})

    assert response.status_code == 404
    assert response.json()["error"] == "Workflow entrypoint not found"
    assert state["events"], "Expected a domain event for rejected ingress"
    event = state["events"][-1]
    assert event["event_type"] == "workflow.ingress.rejected"
    assert event["payload"]["reason"] == "entrypoint_not_found"


def test_workflow_entrypoint_disabled_records_rejected_ingress_event_with_entrypoint_account(
    monkeypatch,
):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "enabled": False,
            "account_id": "acct-entrypoint",
        },
    )

    response = client.post("/webhook/workflows/inventory_sync", json={"title": "Sync inventory"})

    assert response.status_code == 409
    assert response.json()["error"] == "Workflow entrypoint is disabled"
    event = state["events"][-1]
    assert event["account_id"] == "acct-entrypoint"
    assert event["payload"]["reason"] == "entrypoint_disabled"


@pytest.mark.parametrize("workflow_enabled", [False, True], ids=["disabled-owner", "enabled-owner"])
def test_workflow_owner_controls_authenticated_webhook_admission(monkeypatch, workflow_enabled):
    client, state = _build_entrypoint_client(
        monkeypatch, workflow_enabled=workflow_enabled, idempotency_mode="header"
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "owner-admission"},
    )

    if workflow_enabled:
        assert response.status_code == 202, response.text
        assert state["enqueued"] == 1
    else:
        assert response.status_code == 409, response.text
        assert response.json() == {
            "error": "This workflow is turned off. Enable it before sending a webhook.",
            "code": "workflow_disabled",
        }
        assert state["reservation_attempts"] == 0
        assert state["enqueued"] == 0
        assert state["events"][-1]["payload"]["reason"] == "workflow_disabled"
        assert state["workflow_reads"], "Admission must consult the registry owner"


def test_wrong_bearer_cannot_discover_disabled_workflow_owner(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch, workflow_enabled=False)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory"},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == "Invalid bearer token"
    assert "workflow_disabled" not in response.text
    assert state["workflow_reads"] == []
    assert state["reservation_attempts"] == 0
    assert state["enqueued"] == 0


@pytest.mark.parametrize("owner_missing", [True, False], ids=["missing-owner", "lookup-outage"])
def test_workflow_owner_lookup_fails_closed_before_reservation(monkeypatch, owner_missing):
    client, state = _build_entrypoint_client(
        monkeypatch,
        workflow_exists=not owner_missing,
        workflow_lookup_error=None if owner_missing else RuntimeError("private database detail"),
        idempotency_mode="header",
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "unavailable-owner"},
    )

    assert response.status_code == (404 if owner_missing else 503), response.text
    assert response.json()["code"] == (
        "workflow_not_found" if owner_missing else "workflow_lookup_unavailable"
    )
    assert "private database detail" not in response.text
    assert state["reservation_attempts"] == 0
    assert state["enqueued"] == 0


@pytest.mark.parametrize(
    ("auth_mode", "owner_state", "expected_status"),
    [
        ("bearer_token", "disabled", 409),
        ("hmac_sha256", "disabled", 409),
        ("hmac_sha256", "outage", 503),
        ("hmac_sha256", "enabled", 202),
        ("hmac_sha256", "bad-signature", 401),
    ],
)
def test_workflow_owner_multipart_admission_and_cleanup(
    monkeypatch, auth_mode, owner_state, expected_status
):
    from core.application.jobs.submission_admission import SubmissionAdmissionController
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    spools = []
    staging_calls = []
    writes = []
    original_spool = execute_routes._stage_request_body_with_cap
    original_stage = execute_routes._stage_multipart_files
    controller = SubmissionAdmissionController(capacity=1)

    async def spool(request, *, max_body_bytes):
        staged, digest = await original_spool(request, max_body_bytes=max_body_bytes)
        spools.append(staged)
        return staged, digest

    async def stage(*args, **kwargs):
        staging_calls.append(True)
        return await original_stage(*args, **kwargs)

    class _ArtifactPlane:
        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            body = b"".join(chunks)
            writes.append(body)
            return SimpleNamespace(artifact_id=artifact_id), {
                "filename": filename,
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "object_key": f"artifacts/{artifact_id}/{filename}",
            }

        def delete_artifact(self, _artifact_id):
            return True

    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", spool)
    monkeypatch.setattr(execute_routes, "_stage_multipart_files", stage)
    monkeypatch.setattr(execute_routes, "workflow_entrypoint_submission_admission", controller)
    client, state = _build_entrypoint_client(
        monkeypatch,
        workflow_enabled=owner_state == "enabled",
        workflow_lookup_error=RuntimeError("owner storage failed")
        if owner_state == "outage"
        else None,
        idempotency_mode="header",
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "auth_mode": auth_mode,
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "allowed_content_types_jsonb": ["multipart/form-data"],
        },
    )
    boundary = "owner-admission-boundary"
    raw_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "Invoice\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="invoice.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "invoice\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Idempotency-Key": "owner-multipart",
    }
    if auth_mode == "bearer_token":
        headers["Authorization"] = "Bearer token-123"
    else:
        digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()
        headers["X-Workflow-Signature"] = (
            f"sha256={digest}" if owner_state != "bad-signature" else "sha256=" + "0" * 64
        )

    response = client.post("/webhook/workflows/inventory_sync", content=raw_body, headers=headers)

    assert response.status_code == expected_status, response.text
    if expected_status == 202:
        assert state["enqueued"] == 1
        assert writes == [b"invoice"]
        assert staging_calls == [True]
    else:
        assert state["enqueued"] == 0
        assert state["reservation_attempts"] == 0
        assert writes == []
        assert staging_calls == []
    if owner_state == "bad-signature":
        assert state["workflow_reads"] == []
    if auth_mode == "hmac_sha256":
        assert len(spools) == 1, "HMAC needs the capped raw body before authenticating"
        assert spools[0].closed
    else:
        assert spools == [], "Bearer refusal must precede reading the multipart body"
    lease = controller.try_acquire(account_id="next-account", principal_id="next-request")
    assert lease is not None, "Request exit must release its admission lease"
    lease.release()


def test_cancelled_owner_lookup_closes_signed_body_and_releases_capacity(monkeypatch):
    from core.application.jobs.submission_admission import SubmissionAdmissionController
    from core.application.workflows.workflow_entrypoint_models import WorkflowEntrypointPreparation
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes
    from starlette.requests import Request

    spools = []
    original_spool = execute_routes._stage_request_body_with_cap
    original_threadpool = execute_routes.run_in_threadpool
    controller = SubmissionAdmissionController(capacity=1)
    body = b"--cancel-boundary--\r\n"
    digest = hmac.new(b"token-123", body, hashlib.sha256).hexdigest()

    async def spool(request, *, max_body_bytes):
        staged, body_digest = await original_spool(request, max_body_bytes=max_body_bytes)
        spools.append(staged)
        return staged, body_digest

    def check_owner(_workflow_id):
        raise AssertionError("Cancellation must occur at the owner-check await")

    async def threadpool(func, *args, **kwargs):
        if func is check_owner:
            raise asyncio.CancelledError
        return await original_threadpool(func, *args, **kwargs)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhook/workflows/cancel",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=cancel-boundary"),
                (b"x-workflow-signature", f"sha256={digest}".encode()),
            ],
        },
        receive,
    )
    preparation = WorkflowEntrypointPreparation(
        workflow_id="wf.inventory.sync",
        project_id="7",
        entrypoint={
            "auth_mode": "hmac_sha256",
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "allowed_content_types_jsonb": ["multipart/form-data"],
        },
    )
    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", spool)
    monkeypatch.setattr(execute_routes, "run_in_threadpool", threadpool)
    monkeypatch.setattr(execute_routes, "workflow_entrypoint_submission_admission", controller)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            execute_routes._admit_transport_request(
                preparation,
                request=request,
                bl=SimpleNamespace(get_project_secret=lambda **_kwargs: "token-123"),
                entry_key="cancel",
                request_service=SimpleNamespace(check_workflow_admission=check_owner),
            )
        )
    assert len(spools) == 1
    assert spools[0].closed
    lease = controller.try_acquire(account_id="next-account", principal_id="next-request")
    assert lease is not None
    lease.release()


def test_workflow_entrypoint_missing_project_binding_records_rejected_ingress_event(
    monkeypatch,
):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"account_id": "acct-entrypoint"},
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "resolve_project_run_config_for_workflow_project",
        lambda _bl, _workflow_id, _project_id: (None, {}),
    )

    response = client.post("/webhook/workflows/inventory_sync", json={"title": "Sync inventory"})

    assert response.status_code == 404
    assert "No enabled project is linked" in response.json()["error"]
    event = state["events"][-1]
    assert event["account_id"] == "acct-entrypoint"
    assert event["payload"]["reason"] == "workflow_project_binding_not_found"


def test_provider_trigger_create_returns_409_for_duplicate_scope(monkeypatch):
    client = _build_provider_trigger_client(
        monkeypatch,
        create_impl=lambda _bl, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "duplicate key value violates unique constraint "
                '"workflow_provider_triggers_trigger_scope_key"'
            )
        ),
    )

    response = client.post(
        "/api/workflows/wf.inventory.sync/triggers/providers",
        json={
            "project_id": "proj-1",
            "provider_key": "sample_tasks",
            "trigger_type_id": "task-created",
            "display_name": "Sample Tasks task created",
            "enabled": True,
            "trigger_config": {"space_id": "space-1"},
        },
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]


def test_provider_trigger_update_returns_409_for_duplicate_scope(monkeypatch):
    client = _build_provider_trigger_client(
        monkeypatch,
        update_impl=lambda _bl, trigger_id, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                f"duplicate key value violates unique constraint "
                f'"workflow_provider_triggers_trigger_scope_key" for {trigger_id}'
            )
        ),
    )

    response = client.patch(
        "/api/workflows/wf.inventory.sync/triggers/providers/trig-1",
        json={
            "project_id": "proj-1",
            "provider_key": "sample_tasks",
            "trigger_type_id": "task-created",
            "display_name": "Sample Tasks task created",
            "enabled": True,
            "trigger_config": {"space_id": "space-1"},
        },
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["error"]


def test_provider_trigger_update_requires_current_project_scope_before_rebind(monkeypatch):
    called = {"updated": False}
    client = _build_provider_trigger_client(
        monkeypatch,
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-2"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
        update_impl=lambda *_args, **_kwargs: called.__setitem__("updated", True),
    )

    response = client.patch(
        "/api/workflows/wf.inventory.sync/triggers/providers/trig-1",
        json={
            "project_id": "proj-2",
            "provider_key": "sample_tasks",
            "trigger_type_id": "task-created",
            "display_name": "Sample Tasks task created",
            "enabled": True,
            "trigger_config": {"space_id": "space-1"},
        },
    )

    assert response.status_code == 403
    assert called["updated"] is False


def test_workflow_entrypoint_create_enforces_project_scope_for_trigger_management(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-1"},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: set())
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-1"),
            {"workflow_id": workflow_id},
        ),
    )

    client, _state = _build_entrypoint_client(
        monkeypatch,
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-allowed"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "proj-1",
            "description": "test",
            "allowed_content_types": ["application/json"],
        },
    )

    assert response.status_code == 403
    assert "Access denied" in response.text


def test_workflow_entrypoint_deliveries_enforce_project_scope(monkeypatch):
    client, _state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"project_id": "proj-1"},
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-allowed"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.get(
        "/api/workflows/wf.inventory.sync/triggers/webhooks/inventory_sync/deliveries"
    )

    assert response.status_code == 403
    assert "Access denied" in response.text


def test_workflow_entrypoint_deliveries_fail_closed_without_project_binding(monkeypatch):
    client, _state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"project_id": None},
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-allowed"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.get(
        "/api/workflows/wf.inventory.sync/triggers/webhooks/inventory_sync/deliveries"
    )

    assert response.status_code == 409
    assert "no project binding" in response.text


def test_workflow_trigger_listing_filters_webhooks_to_requested_project(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "list_workflow_entrypoints_for_workflow",
        lambda _bl, _workflow_id: [
            {
                "entry_key": "inventory_sync",
                "workflow_id": "wf.inventory.sync",
                "entry_kind": "webhook",
                "project_id": "proj-allowed",
                "enabled": True,
                "allowed_content_types_jsonb": ["application/json"],
            },
            {
                "entry_key": "billing_sync",
                "workflow_id": "wf.inventory.sync",
                "entry_kind": "webhook",
                "project_id": "proj-other",
                "enabled": True,
                "allowed_content_types_jsonb": ["application/json"],
            },
        ],
    )
    monkeypatch.setattr(
        mod.workflow_provider_trigger_repository,
        "list_workflow_provider_triggers",
        lambda _bl, *, workflow_id, project_id: [],
    )
    monkeypatch.setattr(mod, "list_workflow_trigger_catalog", lambda _bl: [])
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    client, _state = _build_entrypoint_client(
        monkeypatch,
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-allowed"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.get("/api/workflows/wf.inventory.sync/triggers?project_id=proj-allowed")

    assert response.status_code == 200
    body = response.json()
    assert [row["entry_key"] for row in body["incoming_webhooks"]] == ["inventory_sync"]


def test_provider_trigger_delete_enforces_project_scope(monkeypatch):
    client = _build_provider_trigger_client(
        monkeypatch,
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-allowed"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.delete("/api/workflows/wf.inventory.sync/triggers/providers/trig-1")

    assert response.status_code == 403
    assert "Access denied" in response.text


def test_workflow_entrypoint_rekey_requires_current_project_scope(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    called = {"regenerated": False}
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "regenerate_workflow_entrypoint_key",
        lambda *_args, **_kwargs: called.__setitem__("regenerated", True),
    )
    client, _state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"project_id": "proj-1"},
        request_scope=lambda _request, **_kwargs: normalize_scope(
            account_id="acct-1",
            allowed_project_ids=frozenset({"proj-2"}),
            allowed_workflow_ids=frozenset({"wf.inventory.sync"}),
        ),
    )

    response = client.post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks/inventory_sync/"
        "regenerate-entry-key?project_id=proj-2"
    )

    assert response.status_code == 403
    assert called["regenerated"] is False


def test_workflow_entrypoint_rejects_invalid_bearer_token(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert state["enqueued"] == 0
    assert state["events"][-1]["event_type"] == "workflow.ingress.rejected"
    assert state["events"][-1]["payload"]["reason"] == "auth_failed"
    assert len(state["logs"]) == 1


def test_workflow_entrypoint_rejects_missing_auth_mode(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "",
            "auth_config_jsonb": {},
            "secret_ref": "",
        },
    )

    response = client.post("/webhook/workflows/inventory_sync", json={"title": "x"})

    assert response.status_code == 503
    assert state["enqueued"] == 0
    assert state["events"][-1]["event_type"] == "workflow.ingress.rejected"
    assert state["events"][-1]["payload"]["reason"] == "auth_mode_not_configured"


def test_workflow_entrypoint_accepts_valid_shared_secret_header(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "shared_secret_header",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Secret",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"X-Workflow-Secret": "token-123"},
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1


def test_workflow_entrypoint_accepts_valid_basic_auth(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "basic_auth",
            "auth_config_jsonb": {
                "username": "workflow",
                "password_secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )
    token = base64.b64encode(b"workflow:token-123").decode("ascii")

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"Authorization": f"Basic {token}"},
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1


def test_workflow_entrypoint_accepts_valid_hmac_signature(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )
    raw_body = b'{"title":"x"}'
    digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Workflow-Signature": f"sha256={digest}",
        },
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1


def test_header_auth_rejects_before_multipart_body_staging(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    stage_body = AsyncMock(return_value=(io.BytesIO(b""), hashlib.sha256(b"").hexdigest()))
    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", stage_body)
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.pdf", b"invoice", "application/pdf")},
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    stage_body.assert_not_called()
    assert state["enqueued"] == 0


def test_shared_admission_limit_rejects_hmac_before_body_staging(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    class _ExhaustedAdmission:
        def try_acquire(self, *, account_id, principal_id):
            assert account_id
            assert principal_id == "inventory_sync:hmac_sha256"
            return

    stage_body = AsyncMock()
    monkeypatch.setattr(
        execute_routes,
        "workflow_entrypoint_submission_admission",
        _ExhaustedAdmission(),
    )
    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", stage_body)
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "allowed_content_types_jsonb": ["multipart/form-data"],
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.pdf", b"invoice", "application/pdf")},
        headers={"X-Workflow-Signature": "sha256=placeholder"},
    )

    assert response.status_code == 429
    assert response.json() == {
        "error": "Workflow submission capacity is exhausted; retry later",
        "code": "submission_admission_limited",
    }
    assert response.headers["Retry-After"] == "2"
    stage_body.assert_not_called()
    assert state["enqueued"] == 0


def test_workflow_entrypoint_hmac_replay_is_deduplicated(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
        },
    )
    raw_body = b'{"title":"hmac-replay"}'
    digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Workflow-Signature": f"sha256={digest}",
    }

    first = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)
    second = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert state["enqueued"] == 1


def test_hmac_replay_reconciles_a_new_primary_idempotency_claim(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        entrypoint_overrides={
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
        },
    )
    raw_body = b'{"title":"hmac-replay"}'
    digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()

    def _request(key: str):
        return client.post(
            "/webhook/workflows/inventory_sync",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Workflow-Signature": f"sha256={digest}",
                "Idempotency-Key": key,
            },
        )

    assert _request("primary-1").status_code == 202
    assert _request("primary-2").status_code == 200
    assert _request("primary-2").status_code == 200
    assert state["enqueued"] == 1


def test_runtime_rejects_mapping_of_configured_hmac_header(monkeypatch):
    raw_body = b'{"title":"x"}'
    digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"title"},
        entrypoint_overrides={
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Dynamic-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "input_mapping_jsonb": {"title": "headers.X-Dynamic-Signature"},
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Dynamic-Signature": f"sha256={digest}",
        },
    )

    assert response.status_code == 400
    assert state["enqueued"] == 0


def _signed_webhook_config(**overrides: Any) -> dict[str, Any]:
    """Stripe-shaped generic verifier config: comma-kv header + timestamped payload."""
    config: dict[str, Any] = {
        "signature": {
            "source": "header",
            "header": "Stripe-Signature",
            "parser": "comma_kv",
            "value_key": "v1",
            "encoding": "hex",
            "allow_multiple": True,
        },
        "timestamp": {
            "source": "signature_param",
            "key": "t",
            "unit": "seconds",
            "tolerance_seconds": 300,
        },
        "signed_payload": {
            "parts": [
                {"source": "timestamp"},
                {"literal": "."},
                {"source": "raw_body"},
            ]
        },
        "secret_ref": "workflow_entrypoint.inventory_sync.secret",
    }
    config.update(overrides)
    return config


def _signed_webhook_header(raw_body: bytes, *, issued_at: int | None = None) -> str:
    timestamp = int(time.time()) if issued_at is None else issued_at
    signed = f"{timestamp}.".encode() + raw_body
    digest = hmac.new(b"token-123", signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_signed_webhook_accepts_a_timestamped_comma_kv_signature(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
        },
    )
    raw_body = b'{"title":"signed"}'

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _signed_webhook_header(raw_body),
        },
    )

    assert response.status_code == 202, response.text
    assert state["enqueued"] == 1


def test_signed_webhook_rejects_a_bad_signature_with_401(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
        },
    )
    raw_body = b'{"title":"signed"}'
    header = _signed_webhook_header(b'{"title":"other"}')

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )

    assert response.status_code == 401
    assert state["enqueued"] == 0


def test_signed_webhook_rejects_an_expired_timestamp_with_401(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
        },
    )
    raw_body = b'{"title":"signed"}'
    header = _signed_webhook_header(raw_body, issued_at=int(time.time()) - 3600)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )

    assert response.status_code == 401
    assert state["enqueued"] == 0


def test_signed_webhook_invalid_server_config_fails_closed_with_503(monkeypatch):
    broken = _signed_webhook_config()
    broken["signature"] = {**broken["signature"], "parser": "rot13"}
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": broken,
        },
    )
    raw_body = b'{"title":"signed"}'

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _signed_webhook_header(raw_body),
        },
    )

    assert response.status_code == 503
    assert state["enqueued"] == 0


def test_signed_webhook_replay_is_deduplicated(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
        },
    )
    raw_body = b'{"title":"signed-replay"}'
    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": _signed_webhook_header(raw_body),
    }

    first = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)
    second = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert state["enqueued"] == 1


def test_signed_webhook_replay_key_is_namespaced_to_the_new_mode():
    from core.entrypoints.api.workflow_entrypoints_execution_auth import (
        _derive_secret_replay_key,
    )

    raw_body = b'{"title":"signed"}'
    request = SimpleNamespace(headers={"Stripe-Signature": _signed_webhook_header(raw_body)})

    replay_key = _derive_secret_replay_key(
        auth_mode="webhook_signature",
        entrypoint={"auth_config_jsonb": _signed_webhook_config()},
        request=request,
        raw_body=raw_body,
        selected_workflow_id="wf.inventory.sync",
    )

    assert replay_key.startswith("webhook-signature:")


def test_signed_webhook_stages_the_request_body_before_authenticating(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    original_stage = execute_routes._stage_request_body_with_cap
    staged_calls: list[int] = []

    async def _spy(request, *, max_body_bytes):
        staged_calls.append(int(max_body_bytes))
        return await original_stage(request, max_body_bytes=max_body_bytes)

    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", _spy)
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
            "allowed_content_types_jsonb": ["multipart/form-data"],
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.pdf", b"invoice", "application/pdf")},
        headers={"Stripe-Signature": f"t={int(time.time())},v1={'a' * 64}"},
    )

    assert response.status_code == 401
    assert staged_calls, "signed webhooks must stage the body before authentication"
    assert state["enqueued"] == 0


def test_signed_webhook_verifies_a_streamed_multipart_body(monkeypatch):
    boundary = "flow-steward-boundary"
    raw_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "invoice\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"title"},
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_body_bytes": 16384,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        content=raw_body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Stripe-Signature": _signed_webhook_header(raw_body),
        },
    )

    assert response.status_code == 202, response.text
    assert state["enqueued"] == 1


def test_runtime_rejects_mapping_of_configured_signature_header(monkeypatch):
    raw_body = b'{"title":"x"}'
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"title"},
        entrypoint_overrides={
            "auth_mode": "webhook_signature",
            "auth_config_jsonb": _signed_webhook_config(),
            "input_mapping_jsonb": {"title": "headers.Stripe-Signature"},
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _signed_webhook_header(raw_body),
        },
    )

    assert response.status_code == 400
    assert state["enqueued"] == 0


def test_idempotency_fingerprint_ignores_undeclared_body_fields(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    fingerprints: list[str] = []

    def _claim(_bl, *, request_fingerprint, **_kwargs):
        fingerprints.append(request_fingerprint)
        if len(fingerprints) == 1:
            return {"claimed": True, "request_id": "owner-1"}
        return {"claimed": False, "job_id": "501", "request_id": "owner-1"}

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
        expected_input_names={"title"},
    )
    monkeypatch.setattr(mod, "_mark_idempotency_reservation_accepted", lambda *_a, **_k: True)

    first = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same", "ignored": "first"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "same-key"},
    )
    second = client.post(
        "/webhook/workflows/inventory_sync",
        json={"ignored": "second", "title": "same"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "same-key"},
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert fingerprints[0] == fingerprints[1]
    assert state["enqueued"] == 1


def test_live_idempotency_claim_never_triggers_redelivery(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    redeliver = MagicMock(return_value=True)

    def _claim(_bl, **_kwargs):
        return {
            "claimed": False,
            "status": "pending",
            "job_id": "501",
            "request_id": "active-owner",
        }

    monkeypatch.setattr(mod, "durable_job_dispatch_confirmed", lambda *_args: False)
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
        redeliver_task=redeliver,
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "live-key"},
    )

    assert response.status_code == 409
    assert state["enqueued"] == 0
    redeliver.assert_not_called()
    logged = state["logs"][-1]
    assert logged["status"] == "rejected"
    assert logged["meta"]["http_status"] == 409
    assert logged["payload"]["reason"] == "idempotency_in_progress"
    assert state["events"][-1]["event_type"] == "workflow.ingress.rejected"


def test_async_entrypoint_offloads_blocking_application_calls(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    invoked: list[str] = []

    async def _tracked_threadpool(callable_, *args, **kwargs):
        invoked.append(getattr(callable_, "__name__", type(callable_).__name__))
        return callable_(*args, **kwargs)

    monkeypatch.setattr(execute_routes, "run_in_threadpool", _tracked_threadpool, raising=False)
    client, _state = _build_entrypoint_client(monkeypatch, idempotency_mode="header")

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "offload-key"},
    )

    assert response.status_code == 202
    assert "prepare" in invoked
    assert "_validate_workflow_entrypoint_auth" in invoked
    assert "execute" in invoked
    assert "record_submission" in invoked


def test_bound_project_resolution_is_offloaded_from_async_route(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    invoked: list[str] = []

    async def _tracked_threadpool(callable_, *args, **kwargs):
        invoked.append(getattr(callable_, "__name__", type(callable_).__name__))
        return callable_(*args, **kwargs)

    def _resolve_bound_project(_bl, workflow_id, project_id):
        assert workflow_id == "wf.inventory.sync"
        assert project_id == "proj-bound"
        return "proj-bound", {"project_name": "Bound", "project_root": "/target"}

    monkeypatch.setattr(execute_routes, "run_in_threadpool", _tracked_threadpool, raising=False)
    monkeypatch.setattr(
        execute_routes.workflow_entrypoint_repository,
        "resolve_project_run_config_for_workflow_project",
        _resolve_bound_project,
    )
    client, _state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"project_id": "proj-bound"},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    assert "prepare" in invoked


def test_validation_exception_is_safe_and_precedes_claim_and_artifact_persistence(monkeypatch):
    secret = "postgresql://admin:super-secret@db.example/app"
    claimed = MagicMock(return_value={"claimed": True, "request_id": "owner-1"})

    class _ArtifactPlane:
        def create_artifact_from_stream(self, **_kwargs):
            raise AssertionError("artifact persistence must not run before validation")

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=claimed,
        artifact_plane=_ArtifactPlane(),
        validation_impl=lambda *_args: (_ for _ in ()).throw(RuntimeError(secret)),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.txt", b"invoice", "text/plain")},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "safe-key"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "Workflow contract service is unavailable; retry later",
        "code": "workflow_contract_unavailable",
    }
    assert secret not in response.text
    claimed.assert_not_called()
    assert state["enqueued"] == 0
    assert secret not in json.dumps(state["logs"])


def test_enqueue_exception_uses_stable_public_error_and_redacted_audit(monkeypatch):
    secret = "amqp://worker:broker-secret@rabbitmq.internal/vhost"
    client, state = _build_entrypoint_client(
        monkeypatch,
        enqueue_impl=MagicMock(side_effect=RuntimeError(secret)),
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": "Workflow dispatch failed",
        "code": "workflow_dispatch_failed",
    }
    assert secret not in response.text
    assert secret not in json.dumps(state["logs"])


def test_artifact_exception_uses_stable_error_and_safe_reservation_failure(monkeypatch):
    mod = _workflow_entrypoint_test_surface()
    secret = "postgresql://storage:super-secret@artifact-db.internal/store"
    failed = MagicMock(return_value=True)
    monkeypatch.setattr(mod, "_mark_idempotency_reservation_failed", failed)

    class _ArtifactPlane:
        def create_artifact_from_stream(self, **_kwargs):
            raise RuntimeError(secret)

        def delete_artifact(self, _artifact_id):
            return True

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=lambda *_args, **_kwargs: {"claimed": True, "request_id": "owner-1"},
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.txt", b"invoice", "text/plain")},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "safe-key"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "Attachment persistence failed",
        "code": "artifact_persistence_failed",
    }
    assert secret not in response.text
    assert secret not in json.dumps(state["logs"])
    assert failed.call_count == 1
    assert secret not in failed.call_args.kwargs["error_text"]


def test_success_response_does_not_expose_artifact_storage_references(monkeypatch):
    class _ArtifactPlane:
        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            body = b"".join(chunks)
            return SimpleNamespace(artifact_id=artifact_id), {
                "filename": filename,
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "object_key": f"private-bucket/{artifact_id}/{filename}",
            }

        def delete_artifact(self, _artifact_id):
            return True

    client, _state = _build_entrypoint_client(
        monkeypatch,
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.txt", b"invoice", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    assert response.json()["file_count"] == 1
    assert "files" not in response.json()
    assert "private-bucket" not in response.text


def test_raw_idempotency_key_never_reaches_job_or_audit_payload(monkeypatch):
    from core.application.idempotency.service import RequestIdempotencyService

    raw_key = "customer-secret-idempotency-key"
    client, state = _build_entrypoint_client(monkeypatch, idempotency_mode="header")

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": raw_key},
    )

    assert response.status_code == 202
    digest = RequestIdempotencyService.digest_key(raw_key)
    assert raw_key not in json.dumps(state["last_enqueue"])
    assert raw_key not in json.dumps(state["logs"])
    assert raw_key not in json.dumps(state["events"])
    assert state["last_enqueue"]["meta"]["idempotency_key_digest"] == digest
    assert state["last_enqueue"]["meta"]["invocation"]["idempotency_key_digest"] == digest
    assert state["logs"][-1]["payload"]["idempotency_key_digest"] == digest

    rejected = client.post(
        "/webhook/workflows/inventory_sync",
        content=b"{not-json",
        headers={
            "Authorization": "Bearer token-123",
            "Content-Type": "application/json",
            "Idempotency-Key": raw_key,
        },
    )

    assert rejected.status_code == 400
    assert state["events"][-1]["payload"]["idempotency_key_digest"] == digest


def test_authenticated_payload_parse_rejection_is_audited(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        content=b"{not-json",
        headers={"Authorization": "Bearer token-123", "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "payload_parse_failed"
    assert state["logs"][-1]["status"] == "rejected"
    assert state["logs"][-1]["payload"]["reason"] == "payload_parse_failed"
    assert state["logs"][-1]["meta"]["auth_outcome"] == "passed"
    assert state["events"][-1]["event_type"] == "workflow.ingress.rejected"


def test_header_recovery_publish_failure_releases_claim_and_returns_503(monkeypatch):
    mod = _workflow_entrypoint_test_surface()
    failed = MagicMock(return_value=True)
    monkeypatch.setattr(mod, "_find_existing_idempotent_job", lambda *_a, **_k: "501")
    monkeypatch.setattr(mod, "durable_job_dispatch_confirmed", lambda *_a, **_k: False)
    monkeypatch.setattr(mod, "_mark_idempotency_reservation_failed", failed)
    client, _state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=lambda *_a, **_k: {
            "claimed": True,
            "recovered": True,
            "request_id": "owner-1",
        },
        redeliver_task=MagicMock(side_effect=RuntimeError("broker down")),
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "recover-key"},
    )

    assert response.status_code == 503
    failed.assert_called_once()
    assert failed.call_args.kwargs["idempotency_key"] == "recover-key"


def test_secret_replay_recovery_publish_failure_releases_replay_claim(monkeypatch):
    mod = _workflow_entrypoint_test_surface()
    failed = MagicMock(return_value=True)
    monkeypatch.setattr(mod, "_find_existing_idempotent_job", lambda *_a, **_k: "501")
    monkeypatch.setattr(mod, "durable_job_dispatch_confirmed", lambda *_a, **_k: False)
    monkeypatch.setattr(mod, "_mark_idempotency_reservation_failed", failed)
    client, _state = _build_entrypoint_client(
        monkeypatch,
        claim_impl=lambda *_a, **_k: {
            "claimed": True,
            "recovered": True,
            "request_id": "owner-1",
        },
        redeliver_task=MagicMock(side_effect=RuntimeError("broker down")),
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "same"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 503
    failed.assert_called_once()
    assert failed.call_args.kwargs["idempotency_key"]


def test_multipart_fingerprint_treats_attachments_as_a_multiset(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    fingerprints: list[str] = []

    def _claim(_bl, *, request_fingerprint, **_kwargs):
        fingerprints.append(request_fingerprint)
        if len(fingerprints) == 1:
            return {"claimed": True, "request_id": "owner-1"}
        return {"claimed": False, "job_id": "501", "request_id": "owner-1"}

    class _ArtifactPlane:
        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            body = b"".join(chunks)
            return SimpleNamespace(artifact_id=artifact_id), {
                "filename": filename,
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "object_key": f"artifacts/{artifact_id}/{filename}",
            }

        def delete_artifact(self, _artifact_id):
            return True

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_body_bytes": 16384,
        },
    )
    monkeypatch.setattr(mod, "_mark_idempotency_reservation_accepted", lambda *_a, **_k: True)
    headers = {"Authorization": "Bearer token-123", "Idempotency-Key": "same-key"}
    first = client.post(
        "/webhook/workflows/inventory_sync",
        files=[
            ("files", ("a.txt", b"a", "text/plain")),
            ("files", ("b.txt", b"b", "text/plain")),
        ],
        headers=headers,
    )
    second = client.post(
        "/webhook/workflows/inventory_sync",
        files=[
            ("files", ("b.txt", b"b", "text/plain")),
            ("files", ("a.txt", b"a", "text/plain")),
        ],
        headers=headers,
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    assert fingerprints[0] == fingerprints[1]
    assert state["enqueued"] == 1


def test_hmac_multipart_is_spooled_without_request_body_cache(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_mod

    original_install = execute_mod._install_staged_request_replay
    observed = {"body_cached": None}

    async def _install(request, staged):
        await original_install(request, staged)
        observed["body_cached"] = isinstance(getattr(request, "_body", None), bytes)

    monkeypatch.setattr(execute_mod, "_install_staged_request_replay", _install)

    class _ArtifactPlane:
        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            body = b"".join(chunks)
            return SimpleNamespace(artifact_id=artifact_id), {
                "filename": filename,
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "object_key": f"artifacts/{artifact_id}/{filename}",
            }

        def delete_artifact(self, _artifact_id):
            return True

    boundary = "flow-steward-boundary"
    raw_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "invoice\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="invoice.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "invoice-body\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    digest = hmac.new(b"token-123", raw_body, hashlib.sha256).hexdigest()
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"title"},
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "auth_mode": "hmac_sha256",
            "auth_config_jsonb": {
                "header_name": "X-Workflow-Signature",
                "prefix": "sha256=",
                "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            },
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_body_bytes": 16384,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        content=raw_body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Workflow-Signature": f"sha256={digest}",
        },
    )

    assert response.status_code == 202, response.text
    assert observed["body_cached"] is False
    assert state["enqueued"] == 1


def test_workflow_entrypoint_rejects_disallowed_content_type(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data="plain text",
        headers={"Authorization": "Bearer token-123", "Content-Type": "text/plain"},
    )

    assert response.status_code == 415
    assert state["enqueued"] == 0


def test_workflow_entrypoint_rejects_source_ip_outside_allowlist(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {"exact_ips": ["10.0.0.1"]},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 403
    assert state["enqueued"] == 0


def test_workflow_entrypoint_allows_source_ip_inside_cidr_allowlist(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {"cidrs": ["10.0.0.0/8"]},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )
    monkeypatch.setattr(mod, "_request_ip_for_security", lambda _request: "10.4.8.9")

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1


def test_workflow_entrypoint_source_ip_ignores_forwarded_headers_by_default(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {"exact_ips": ["10.0.0.1"]},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={
            "Authorization": "Bearer token-123",
            "X-Forwarded-For": "10.0.0.1",
            "X-Real-IP": "10.0.0.1",
        },
    )

    assert response.status_code == 403
    assert state["enqueued"] == 0


def test_workflow_entrypoint_partial_mapping_keeps_automap_for_other_expected_inputs(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch, expected_input_names={"amount", "email"})
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {"amount": "headers.x-amount"},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync?email=query@example.com",
        json={"email": "body@example.com"},
        headers={"Authorization": "Bearer token-123", "x-amount": "42"},
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1
    payload = state["last_enqueue"]["extra_state"]["inputs"] if state["last_enqueue"] else {}
    assert payload["amount"] == "42"
    assert payload["email"] == "body@example.com"


def test_workflow_entrypoint_header_mapping_is_case_insensitive(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    client, state = _build_entrypoint_client(monkeypatch, expected_input_names={"event_type"})
    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entry_key": "inventory_sync",
            "workflow_id": "wf.inventory.sync",
            "project_id": "7",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "input_mapping_jsonb": {"event_type": "headers.X-Event-Type"},
            "secret_ref": "workflow_entrypoint.inventory_sync.secret",
            "response_mode": "accepted",
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "x"},
        headers={"Authorization": "Bearer token-123", "x-event-type": "invoice.created"},
    )

    assert response.status_code == 202
    payload = state["last_enqueue"]["extra_state"]["inputs"] if state["last_enqueue"] else {}
    assert payload["event_type"] == "invoice.created"


def test_workflow_entrypoint_accepts_form_urlencoded_payload_and_automaps_inputs(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"email", "amount"},
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["application/x-www-form-urlencoded"],
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data={"email": "form@example.com", "amount": "42"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    payload = state["last_enqueue"]["extra_state"]["inputs"] if state["last_enqueue"] else {}
    assert payload["email"] == "form@example.com"
    assert payload["amount"] == "42"


def test_workflow_entrypoint_accepts_text_plain_with_explicit_body_mapping(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names={"payload_text"},
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["text/plain"],
            "input_mapping_jsonb": {"payload_text": "body.body"},
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data="sync inventory now",
        headers={"Authorization": "Bearer token-123", "Content-Type": "text/plain"},
    )

    assert response.status_code == 202
    payload = state["last_enqueue"]["extra_state"]["inputs"] if state["last_enqueue"] else {}
    assert payload["payload_text"] == "sync inventory now"


def test_workflow_entrypoint_rejects_request_body_above_limit(monkeypatch):
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"max_body_bytes": 8},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        data=b'{"title":"too long"}',
        headers={"Authorization": "Bearer token-123", "Content-Type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert state["enqueued"] == 0


def test_workflow_entrypoint_rejects_chunked_body_above_limit_before_buffering(monkeypatch):
    # FS-DEEP-CAND-037 negative: a request with no/invalid Content-Length (chunked
    # transfer) must be rejected with 413 while streaming, before the whole body is
    # buffered and before payload parsing/enqueue.
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"max_body_bytes": 16},
    )

    def _oversize_chunks():
        yield b'{"title":"' + b"A" * 512 + b'"}'

    response = client.post(
        "/webhook/workflows/inventory_sync",
        content=_oversize_chunks(),
        headers={"Authorization": "Bearer token-123", "Content-Type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert state["enqueued"] == 0


def test_workflow_entrypoint_accepts_chunked_body_within_limit(monkeypatch):
    # FS-DEEP-CAND-037 positive: a chunked (no Content-Length) body within the limit
    # is read successfully and enqueued.
    client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={"max_body_bytes": 4096},
    )

    def _small_chunks():
        yield b'{"title":"ok"}'

    response = client.post(
        "/webhook/workflows/inventory_sync",
        content=_small_chunks(),
        headers={"Authorization": "Bearer token-123", "Content-Type": "application/json"},
    )

    assert response.status_code == 202
    assert state["enqueued"] == 1


def test_workflow_entrypoint_reusable_secret_replay_is_rejected(monkeypatch):
    # FS-DEEP-CAND-052/054 negative: a reusable bearer-token webhook with the default
    # idempotency_mode "none" must still be replay-protected. A duplicate delivery
    # (same credential + same body) is rejected/deduplicated, not enqueued twice.
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="none",
    )
    raw_body = b'{"title":"reusable-replay"}'
    headers = {"Authorization": "Bearer token-123", "Content-Type": "application/json"}

    first = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)
    second = client.post("/webhook/workflows/inventory_sync", data=raw_body, headers=headers)

    assert first.status_code == 202
    assert second.status_code in (200, 409)
    assert state["enqueued"] == 1


def test_workflow_entrypoint_reusable_secret_distinct_deliveries_each_enqueue(monkeypatch):
    # FS-DEEP-CAND-052/054 positive: the first delivery works and a genuinely distinct
    # delivery (different body) is enqueued separately rather than being suppressed.
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="none",
    )
    headers = {"Authorization": "Bearer token-123", "Content-Type": "application/json"}

    first = client.post(
        "/webhook/workflows/inventory_sync",
        data=b'{"title":"one"}',
        headers=headers,
    )
    second = client.post(
        "/webhook/workflows/inventory_sync",
        data=b'{"title":"two"}',
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert state["enqueued"] == 2


def test_workflow_entrypoint_rejects_uploaded_file_above_limit(monkeypatch):
    class _ArtifactPlane:
        def create_artifact(self, **_kwargs):
            raise AssertionError("artifact persistence should not run for oversize files")

    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_file_bytes": 4,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("oversize.txt", b"12345", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 413, response.text
    assert state["enqueued"] == 0


def test_workflow_entrypoint_rejects_uploaded_files_when_max_files_is_zero(monkeypatch):
    class _ArtifactPlane:
        def create_artifact(self, **_kwargs):
            raise AssertionError("artifact persistence should not run when uploads are disallowed")

    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_files": 0,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("sample.txt", b"1234", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 413, response.text
    assert state["enqueued"] == 0


def test_workflow_entrypoint_applies_configured_limits_during_multipart_parse(monkeypatch):
    from starlette.requests import Request

    observed: dict[str, int] = {}
    original_form = Request.form

    async def _capturing_form(self, **kwargs):
        observed.update(kwargs)
        return await original_form(self, **kwargs)

    monkeypatch.setattr(Request, "form", _capturing_form)
    client, _state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_files": 2,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"title": (None, "Inventory sync")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202, response.text
    assert observed == {"max_files": 2, "max_fields": 32, "max_part_size": 262_144}


def test_workflow_entrypoint_holds_shared_admission_through_multipart_lifecycle(monkeypatch):
    from core.application.jobs.submission_admission import SubmissionAdmissionController
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes
    from starlette.requests import Request

    controller = SubmissionAdmissionController(capacity=1)
    original_form = Request.form

    async def _assert_admitted_form(self, **kwargs):
        assert (
            controller.try_acquire(
                account_id="competing-account",
                principal_id="competing-principal",
            )
            is None
        )
        return await original_form(self, **kwargs)

    monkeypatch.setattr(Request, "form", _assert_admitted_form)
    monkeypatch.setattr(
        execute_routes,
        "workflow_entrypoint_submission_admission",
        controller,
        raising=False,
    )
    client, _state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"title": (None, "Inventory sync")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202, response.text
    released = controller.try_acquire(
        account_id="next-account",
        principal_id="next-principal",
    )
    assert released is not None
    released.release()


def test_workflow_entrypoint_times_out_while_receiving_multipart_body(monkeypatch):
    from core.entrypoints.api import workflow_entrypoints_routes_execute as execute_routes

    async def _blocked_stage(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        raise AssertionError("body staging should have timed out")

    monkeypatch.setattr(execute_routes, "_BODY_READ_TIMEOUT_SECONDS", 0.001, raising=False)
    monkeypatch.setattr(execute_routes, "_stage_request_body_with_cap", _blocked_stage)
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("sample.txt", b"1234", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 408, response.text
    assert response.json()["code"] == "request_body_timeout"
    assert state["enqueued"] == 0


def test_secret_replay_duplicate_is_resolved_before_multipart_persistence(monkeypatch):
    class _ArtifactPlane:
        def create_artifact(self, **_kwargs):
            raise AssertionError("duplicate replay must not persist an artifact")

    def _existing_claim(_bl, **_kwargs):
        return {
            "claimed": False,
            "status": "accepted",
            "outcome": "replay",
            "job_id": "501",
        }

    client, state = _build_entrypoint_client(
        monkeypatch,
        claim_impl=_existing_claim,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.pdf", b"invoice", "application/pdf")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 200
    assert response.json()["deduplicated"] is True
    assert state["enqueued"] == 0


def test_multipart_partial_artifacts_are_rolled_back(monkeypatch):
    class _ArtifactPlane:
        def __init__(self) -> None:
            self.created: list[str] = []
            self.deleted: list[str] = []

        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            self.created.append(artifact_id)
            if len(self.created) == 2:
                raise RuntimeError("storage unavailable")
            body = b"".join(chunks)
            return (
                SimpleNamespace(artifact_id=artifact_id),
                {
                    "filename": filename,
                    "content_type": "text/plain",
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "object_key": f"artifacts/{artifact_id}/{filename}",
                },
            )

        def delete_artifact(self, artifact_id):
            self.deleted.append(str(artifact_id))
            return True

    plane = _ArtifactPlane()
    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        artifact_plane=plane,
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("files", ("two.txt", b"two", "text/plain")),
        ],
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 503
    assert state["enqueued"] == 0
    assert plane.deleted == plane.created


def test_multipart_recoverable_publish_failure_preserves_durable_artifacts(monkeypatch):
    from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

    class _ArtifactPlane:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            body = b"".join(chunks)
            return SimpleNamespace(artifact_id=artifact_id), {
                "filename": filename,
                "content_type": "text/plain",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "object_key": f"artifacts/{artifact_id}/{filename}",
            }

        def delete_artifact(self, artifact_id):
            self.deleted.append(str(artifact_id))
            return True

    failure = QueueTransportUnavailableError("RabbitMQ task queue is unavailable")
    failure.recoverable = True
    failure.job_id = "501"
    plane = _ArtifactPlane()
    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        expected_input_names=set(),
        artifact_plane=plane,
        enqueue_impl=MagicMock(side_effect=failure),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("invoice.txt", b"invoice", "text/plain")},
        headers={"Authorization": "Bearer token-123", "Idempotency-Key": "recover-1"},
    )

    assert response.status_code == 503
    assert state["enqueued"] == 1
    assert plane.deleted == []


def test_multipart_artifact_identity_is_journaled_before_storage_write(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    events: list[tuple[str, str]] = []
    journals: list[dict[str, Any]] = []

    class _ArtifactPlane:
        def create_artifact_from_stream(self, *, artifact_id, filename, chunks, **_kwargs):
            events.append(("create", artifact_id))
            body = b"".join(chunks)
            return (
                SimpleNamespace(artifact_id=artifact_id),
                {
                    "filename": filename,
                    "content_type": "text/plain",
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "object_key": f"artifacts/{artifact_id}/{filename}",
                },
            )

        def delete_artifact(self, _artifact_id):
            return True

    client, _state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={"allowed_content_types_jsonb": ["multipart/form-data"]},
    )

    def _journal(*_args, journal, **_kwargs):
        journals.append(dict(journal))
        events.append(("journal", journal["pending_artifact_ids"][-1]))
        return True

    monkeypatch.setattr(mod, "_journal_idempotency_reservation", _journal)
    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("one.txt", b"one", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202, response.text
    assert events[0][0] == "journal"
    assert events[1] == ("create", events[0][1])
    assert journals[0]["pending_artifacts"] == [
        {
            "artifact_id": events[0][1],
            "object_key": f"artifacts/{events[0][1]}/one.txt",
        }
    ]


def test_workflow_entrypoint_rejects_path_like_multipart_filename(monkeypatch):
    class _ArtifactPlane:
        def create_artifact(self, **_kwargs):
            raise AssertionError("artifact persistence should not run for unsafe filenames")

    client, state = _build_entrypoint_client(
        monkeypatch,
        expected_input_names=set(),
        artifact_plane=_ArtifactPlane(),
        entrypoint_overrides={
            "allowed_content_types_jsonb": ["multipart/form-data"],
            "max_files": 2,
        },
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        files={"files": ("../escape.txt", b"1234", "text/plain")},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 400
    assert state["enqueued"] == 0


def test_workflow_entrypoint_rejects_empty_allowed_content_type_config(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": None},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: set())
    monkeypatch.setattr(
        mod, "_resolve_project_run_config_for_workflow", lambda _bl, _workflow_id: ("p1", {})
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "create_workflow_entrypoint",
        lambda *args, **kwargs: kwargs,
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def save_project_secret(self, **kwargs):
            return None

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "proj-3",
            "description": "test",
            "allowed_content_types": [],
        },
    )

    assert response.status_code == 400
    assert "At least one allowed content type" in response.json()["error"]

    invalid_auth = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "proj-3",
            "auth_mode": "unsupported",
            "allowed_content_types": ["application/json"],
        },
    )
    assert invalid_auth.status_code == 400
    assert invalid_auth.json()["code"] == "workflow_entrypoint_validation_failed"


def test_workflow_entrypoint_create_uses_explicit_project_binding_for_secret_storage(
    monkeypatch,
):
    mod = _workflow_entrypoint_test_surface()

    saved_secret: dict[str, str] = {}
    created_fields: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": None},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: set())
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or ""),
            {"workflow_id": workflow_id},
        ),
    )

    def _create_entrypoint(*_args, **kwargs):
        created_fields.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "create_workflow_entrypoint",
        _create_entrypoint,
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "enabled": True}

        def save_project_secret(self, **kwargs):
            saved_secret.update({str(key): str(value) for key, value in kwargs.items()})

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "proj-3",
            "description": "test",
            "allowed_content_types": ["application/json"],
        },
    )

    assert response.status_code == 201
    assert saved_secret["project_id"] == "proj-3"
    assert created_fields["account_id"] == DEFAULT_ACCOUNT_ID


def test_workflow_entrypoint_create_rejects_blank_project_id_before_resolution(monkeypatch):
    mod = _workflow_entrypoint_test_surface()
    resolver = MagicMock()
    monkeypatch.setattr(mod, "_resolve_provider_trigger_project_binding", resolver)

    app = FastAPI()
    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: object(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={"project_id": "   ", "allowed_content_types": ["application/json"]},
    )

    assert response.status_code == 422
    resolver.assert_not_called()


def test_workflow_entrypoint_create_returns_precise_error_for_missing_selected_project_binding(
    monkeypatch,
):
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": None},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: set())
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (None, {}),
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()
    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: object(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "proj-3",
            "description": "test",
            "allowed_content_types": ["application/json"],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "Workflow is not linked to the selected project"


def test_workflow_entrypoint_create_rejects_unresolvable_bound_project(monkeypatch):
    """A resolved project that cannot actually be loaded (orphaned binding) must be
    rejected at create time so the webhook never accepts public traffic that would
    later die with 'Project lookup unavailable'."""
    mod = _workflow_entrypoint_test_surface()

    saved_secret: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": None},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: set())
    # Resolver returns a project id (e.g. from an enabled project_workflows row) ...
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: ("ghost-project", {}),
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True, errors=[], interface={"enabled": True, "channel": channel}
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        # ... but get_project cannot load it (deleted / orphaned) -> reject.
        def get_project(self, _project_id):
            return None

        def save_project_secret(self, **kwargs):
            saved_secret.update(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/wf.inventory.sync/triggers/webhooks",
        json={
            "project_id": "ghost-project",
            "description": "test",
            "allowed_content_types": ["application/json"],
        },
    )

    assert response.status_code == 409
    assert "unavailable" in response.json()["error"].lower()
    # The broken entrypoint's secret was never stored.
    assert not saved_secret


@pytest.mark.parametrize(
    ("auth_mode", "auth_config", "expected_auth_config", "expected_reveal_fields"),
    [
        (
            "bearer_token",
            {},
            lambda entry_key: {
                "secret_ref": f"workflow_entrypoint.{entry_key}.secret.bearer_token",
            },
            {"token"},
        ),
        (
            "basic_auth",
            {"username": "opsbot"},
            lambda entry_key: {
                "username": "opsbot",
                "password_secret_ref": f"workflow_entrypoint.{entry_key}.secret.basic_auth",
            },
            {"username", "password"},
        ),
        (
            "shared_secret_header",
            {"header_name": "X-Demo-Secret"},
            lambda entry_key: {
                "header_name": "X-Demo-Secret",
                "secret_ref": f"workflow_entrypoint.{entry_key}.secret.shared_secret_header",
            },
            {"header_name", "secret"},
        ),
        (
            "hmac_sha256",
            {"header_name": "X-Demo-Signature", "prefix": "sha256="},
            lambda entry_key: {
                "header_name": "X-Demo-Signature",
                "prefix": "sha256=",
                "secret_ref": f"workflow_entrypoint.{entry_key}.secret.hmac_sha256",
            },
            {"header_name", "prefix", "secret"},
        ),
    ],
)
def test_create_workflow_webhook_trigger_persists_full_security_contract(
    monkeypatch,
    auth_mode,
    auth_config,
    expected_auth_config,
    expected_reveal_fields,
):
    mod = _workflow_entrypoint_test_surface()

    captured_create: dict[str, Any] = {}
    saved_secret: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-demo"},
    )
    monkeypatch.setattr(
        mod,
        "_expected_input_names",
        lambda _workflow_id: {"email", "event_type", "source", "payload_text"},
    )
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-demo"),
            {"project_name": "Demo", "project_root": "/tmp/demo", "workflow_id": workflow_id},
        ),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "create_workflow_entrypoint",
        lambda _bl, **kwargs: (
            captured_create.update(kwargs)
            or {
                "entrypoint_id": "ep-demo",
                "created_at": "2026-05-12T00:00:00Z",
                "updated_at": "2026-05-12T00:00:00Z",
                **kwargs,
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "enabled": True}

        def save_project_secret(self, **kwargs):
            saved_secret.update(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/sample_incoming_webhook_router/triggers/webhooks",
        json={
            "project_id": "proj-demo",
            "description": "Demo webhook",
            "enabled": False,
            "auth_mode": auth_mode,
            "auth_config": auth_config,
            "source_restrictions": {
                "exact_ips": ["203.0.113.10"],
                "cidrs": ["203.0.113.0/24"],
            },
            "allowed_content_types": [
                "application/x-www-form-urlencoded",
                "text/plain",
            ],
            "max_body_bytes": 2048,
            "max_file_bytes": 8192,
            "max_files": 3,
            "idempotency_mode": "header",
            "response_mode": "ok",
            "input_mapping": {
                "email": "form.email",
                "event_type": "headers.x-event-type",
                "source": "query.source",
                "payload_text": "body.body",
            },
        },
    )

    assert response.status_code == 201
    payload = response.json()
    entry_key = str(payload["entry_key"])

    assert captured_create["workflow_id"] == "sample_incoming_webhook_router"
    assert captured_create["enabled"] is False
    assert captured_create["project_id"] == "proj-demo"
    assert captured_create["auth_mode"] == auth_mode
    assert captured_create["auth_config_jsonb"] == expected_auth_config(entry_key)
    assert captured_create["source_restrictions_jsonb"] == {
        "exact_ips": ["203.0.113.10"],
        "cidrs": ["203.0.113.0/24"],
    }
    assert captured_create["allowed_content_types_jsonb"] == [
        "application/x-www-form-urlencoded",
        "text/plain",
    ]
    assert captured_create["max_body_bytes"] == 2048
    assert captured_create["max_file_bytes"] == 8192
    assert captured_create["max_files"] == 3
    assert captured_create["idempotency_mode"] == "header"
    assert captured_create["response_mode"] == "ok"
    assert captured_create["input_mapping_jsonb"] == {
        "email": "form.email",
        "event_type": "headers.x-event-type",
        "source": "query.source",
        "payload_text": "body.body",
    }
    assert captured_create["account_id"] == DEFAULT_ACCOUNT_ID

    expected_secret_ref = (
        captured_create["auth_config_jsonb"].get("password_secret_ref")
        if auth_mode == "basic_auth"
        else captured_create["auth_config_jsonb"].get("secret_ref")
    )
    assert saved_secret["project_id"] == "proj-demo"
    assert saved_secret["secret_key"] == expected_secret_ref
    assert str(saved_secret["secret_value"]).strip()

    assert payload["public_path"] == f"/webhook/workflows/{entry_key}"
    assert payload["public_url"].endswith(f"/webhook/workflows/{entry_key}")
    assert payload["project_id"] == "proj-demo"
    assert payload["credential_reveal"]["auth_mode"] == auth_mode
    assert expected_reveal_fields.issubset(set(payload["credential_reveal"]))


def _build_webhook_admin_client(
    monkeypatch,
    *,
    captured_create: dict[str, Any],
    saved_secret: dict[str, Any],
) -> TestClient:
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-demo"},
    )
    monkeypatch.setattr(mod, "_expected_input_names", lambda _workflow_id: {"title"})
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-demo"),
            {"project_name": "Demo", "project_root": "/tmp/demo", "workflow_id": workflow_id},
        ),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "create_workflow_entrypoint",
        lambda _bl, **kwargs: (
            captured_create.update(kwargs)
            or {
                "entrypoint_id": "ep-demo",
                "created_at": "2026-05-12T00:00:00Z",
                "updated_at": "2026-05-12T00:00:00Z",
                **kwargs,
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "enabled": True}

        def save_project_secret(self, **kwargs):
            saved_secret.update(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )
    return TestClient(app)


def _signed_webhook_create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": "proj-demo",
        "auth_mode": "webhook_signature",
        "auth_config": _signed_webhook_config(),
        "allowed_content_types": ["application/json"],
    }
    body["auth_config"].pop("secret_ref", None)
    body.update(overrides)
    return body


def test_create_signed_webhook_trigger_reveals_a_generated_secret(monkeypatch):
    captured_create: dict[str, Any] = {}
    saved_secret: dict[str, Any] = {}
    client = _build_webhook_admin_client(
        monkeypatch, captured_create=captured_create, saved_secret=saved_secret
    )

    response = client.post(
        "/api/workflows/demo_signed_webhook/triggers/webhooks",
        json=_signed_webhook_create_body(),
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    entry_key = str(payload["entry_key"])
    assert captured_create["auth_mode"] == "webhook_signature"
    assert captured_create["auth_config_jsonb"]["signature"]["header"] == "Stripe-Signature"
    assert captured_create["auth_config_jsonb"]["secret_ref"] == (
        f"workflow_entrypoint.{entry_key}.secret.webhook_signature"
    )
    assert saved_secret["secret_key"] == captured_create["auth_config_jsonb"]["secret_ref"]
    reveal = payload["credential_reveal"]
    assert reveal["auth_mode"] == "webhook_signature"
    assert reveal["header_name"] == "Stripe-Signature"
    assert reveal["parser"] == "comma_kv"
    assert reveal["encoding"] == "hex"
    assert reveal["value_key"] == "v1"
    assert reveal["timestamp_source"] == "signature_param"
    assert reveal["secret"] == saved_secret["secret_value"]
    assert "credential_configured" not in payload


def test_create_signed_webhook_trigger_stores_an_imported_secret_without_revealing_it(monkeypatch):
    captured_create: dict[str, Any] = {}
    saved_secret: dict[str, Any] = {}
    client = _build_webhook_admin_client(
        monkeypatch, captured_create=captured_create, saved_secret=saved_secret
    )

    response = client.post(
        "/api/workflows/demo_signed_webhook/triggers/webhooks",
        json=_signed_webhook_create_body(signing_secret="whsec_provider_issued"),
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert saved_secret["secret_value"] == "whsec_provider_issued"  # pragma: allowlist secret
    assert payload["credential_configured"] is True
    assert "credential_reveal" not in payload
    assert "signing_secret" not in payload
    assert "signing_secret" not in captured_create
    assert "signing_secret" not in captured_create["auth_config_jsonb"]
    assert "whsec_provider_issued" not in response.text


def test_create_rejects_an_imported_signing_secret_on_a_non_signed_auth_mode(monkeypatch):
    saved_secret: dict[str, Any] = {}
    client = _build_webhook_admin_client(monkeypatch, captured_create={}, saved_secret=saved_secret)

    response = client.post(
        "/api/workflows/demo_signed_webhook/triggers/webhooks",
        json={
            "project_id": "proj-demo",
            "auth_mode": "bearer_token",
            "signing_secret": "whsec_provider_issued",  # pragma: allowlist secret
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "workflow_entrypoint_validation_failed"
    assert saved_secret == {}


def test_signed_webhook_create_request_rejects_an_unsupported_verifier_config(monkeypatch):
    client = _build_webhook_admin_client(monkeypatch, captured_create={}, saved_secret={})
    body = _signed_webhook_create_body()
    body["auth_config"]["signature"]["parser"] = "rot13"

    response = client.post("/api/workflows/demo_signed_webhook/triggers/webhooks", json=body)

    assert response.status_code == 400
    assert response.json()["code"] == "workflow_entrypoint_validation_failed"


def test_update_workflow_webhook_trigger_rotates_secret_and_persists_updated_fields(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    saved_secret: dict[str, Any] = {}
    captured_update: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entrypoint_id": "ep-1",
            "entry_key": "inventory_sync",
            "workflow_id": "sample_incoming_webhook_router",
            "project_id": "proj-demo",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {
                "secret_ref": "workflow_entrypoint.inventory_sync.secret.bearer_token",
            },
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "secret_ref": "workflow_entrypoint.inventory_sync.secret.bearer_token",
            "idempotency_mode": "header",
            "input_mapping_jsonb": {"email": "body.email"},
            "response_mode": "accepted",
            "description": "Original",
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-1"},
    )
    monkeypatch.setattr(
        mod,
        "_expected_input_names",
        lambda _workflow_id: {"email", "payload_text"},
    )
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-demo"),
            {"project_name": "Demo", "project_root": "/tmp/demo", "workflow_id": workflow_id},
        ),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "update_workflow_entrypoint",
        lambda _bl, entry_key, **kwargs: (
            captured_update.update({"entry_key": entry_key, **kwargs})
            or {
                "entrypoint_id": "ep-1",
                "workflow_id": "sample_incoming_webhook_router",
                "entry_key": entry_key,
                "created_at": "2026-05-12T00:00:00Z",
                "updated_at": "2026-05-12T00:10:00Z",
                **kwargs,
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "enabled": True}

        def save_project_secret(self, **kwargs):
            saved_secret.update(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).patch(
        "/api/workflows/sample_incoming_webhook_router/triggers/webhooks/inventory_sync",
        json={
            "project_id": "proj-demo",
            "enabled": False,
            "description": "Updated",
            "auth_mode": "hmac_sha256",
            "auth_config": {
                "header_name": "X-Demo-Signature",
                "prefix": "sha256=",
            },
            "source_restrictions": {
                "exact_ips": ["198.51.100.20"],
                "cidrs": ["198.51.100.0/24"],
            },
            "allowed_content_types": ["text/plain"],
            "max_body_bytes": 64,
            "max_file_bytes": 128,
            "max_files": 0,
            "idempotency_mode": "none",
            "response_mode": "no_content",
            "input_mapping": {
                "email": "headers.x-user-email",
                "payload_text": "body.body",
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert captured_update["entry_key"] == "inventory_sync"
    assert captured_update["enabled"] is False
    assert captured_update["auth_mode"] == "hmac_sha256"
    assert captured_update["auth_config_jsonb"] == {
        "header_name": "X-Demo-Signature",
        "prefix": "sha256=",
        "secret_ref": "workflow_entrypoint.inventory_sync.secret.hmac_sha256",
    }
    assert captured_update["source_restrictions_jsonb"] == {
        "exact_ips": ["198.51.100.20"],
        "cidrs": ["198.51.100.0/24"],
    }
    assert captured_update["allowed_content_types_jsonb"] == ["text/plain"]
    assert captured_update["max_body_bytes"] == 64
    assert captured_update["max_file_bytes"] == 128
    assert captured_update["max_files"] == 0
    assert captured_update["idempotency_mode"] == "none"
    assert captured_update["response_mode"] == "no_content"
    assert captured_update["input_mapping_jsonb"] == {
        "email": "headers.x-user-email",
        "payload_text": "body.body",
    }
    assert captured_update["description"] == "Updated"

    assert saved_secret["project_id"] == "proj-demo"
    assert saved_secret["secret_key"] == "workflow_entrypoint.inventory_sync.secret.hmac_sha256"
    assert str(saved_secret["secret_value"]).strip()
    assert payload["credential_reveal"]["auth_mode"] == "hmac_sha256"
    assert payload["credential_reveal"]["header_name"] == "X-Demo-Signature"
    assert payload["credential_reveal"]["prefix"] == "sha256="
    assert payload["project_id"] == "proj-demo"
    assert str(payload["credential_reveal"]["secret"]).strip()

    invalid_auth = TestClient(app).patch(
        "/api/workflows/sample_incoming_webhook_router/triggers/webhooks/inventory_sync",
        json={"auth_mode": "unsupported"},
    )
    assert invalid_auth.status_code == 400
    assert invalid_auth.json()["code"] == "workflow_entrypoint_validation_failed"


def test_rotate_workflow_webhook_secret_preserves_schema_project_binding(monkeypatch):
    """Secret rotation keeps the canonical schema-owned project binding."""
    mod = _workflow_entrypoint_test_surface()
    from core.entrypoints.api import workflow_entrypoints_registry as reg_mod

    saved_secret: dict[str, Any] = {}
    captured_update: dict[str, Any] = {}

    def _interface_stub():
        return SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True, errors=[], interface={"enabled": True, "channel": channel}
            ),
        )

    monkeypatch.setattr(mod, "WorkflowInterfaceService", _interface_stub)
    monkeypatch.setattr(reg_mod, "WorkflowInterfaceService", _interface_stub)

    monkeypatch.setattr(
        mod,
        "_load_workflow_entrypoint",
        lambda _bl, _entry_key: {
            "entrypoint_id": "ep-1",
            "entry_key": "inventory_sync",
            "workflow_id": "sample_incoming_webhook_router",
            "project_id": "proj-demo",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {
                "secret_ref": "workflow_entrypoint.inventory_sync.secret.bearer_token",
                "legacy_field": "drop-me",
            },
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "secret_ref": "workflow_entrypoint.inventory_sync.secret.bearer_token",
            "idempotency_mode": "header",
            "input_mapping_jsonb": {},
            "response_mode": "accepted",
            "description": "Original",
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_workflow_manifest",
        lambda workflow_id, **_kwargs: {"workflow_id": workflow_id, "account_id": "acct-1"},
    )
    monkeypatch.setattr(
        mod,
        "_resolve_provider_trigger_project_binding",
        lambda _bl, *, workflow_id, explicit_project_id=None: (
            str(explicit_project_id or "proj-demo"),
            {"project_name": "Demo", "project_root": "/tmp/demo", "workflow_id": workflow_id},
        ),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "update_workflow_entrypoint",
        lambda _bl, entry_key, **kwargs: (
            captured_update.update({"entry_key": entry_key, **kwargs})
            or {
                "entrypoint_id": "ep-1",
                "workflow_id": "sample_incoming_webhook_router",
                "entry_key": entry_key,
                "created_at": "2026-05-12T00:00:00Z",
                "updated_at": "2026-05-12T00:10:00Z",
                **kwargs,
            }
        ),
    )

    app = FastAPI()

    class _BL:
        conn = object()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": DEFAULT_ACCOUNT_ID, "enabled": True}

        def save_project_secret(self, **kwargs):
            saved_secret.update(kwargs)

    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _BL(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/workflows/sample_incoming_webhook_router/triggers/webhooks/inventory_sync/rotate-secret",
        json={"project_id": "proj-demo"},
    )

    assert response.status_code == 200
    payload = response.json()

    assert captured_update["project_id"] == "proj-demo"
    assert "_project_binding_id" not in captured_update["auth_config_jsonb"]
    assert captured_update["auth_config_jsonb"]["secret_ref"]
    # Response exposes the real project_id (was null before the fix).
    assert payload["project_id"] == "proj-demo"
    # New secret is stored under the correct project, enabling later trigger lookup.
    assert saved_secret["project_id"] == "proj-demo"
    assert str(saved_secret["secret_value"]).strip()


def test_workflow_entrypoint_runtime_uses_schema_project_binding_for_secret_lookup(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    _client, state = _build_entrypoint_client(
        monkeypatch,
        entrypoint_overrides={
            "project_id": "11",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
        },
    )

    monkeypatch.setattr(
        mod,
        "_resolve_project_run_config_for_workflow",
        lambda _bl, _workflow_id: ("7", {"project_name": "Wrong", "project_root": "/wrong"}),
    )
    monkeypatch.setattr(
        mod.workflow_entrypoint_repository,
        "resolve_project_run_config_for_workflow_project",
        lambda _bl, _workflow_id, project_id: (
            str(project_id),
            {"project_name": "Pinned", "project_root": "/pinned"},
        ),
    )

    class _PinnedSecretBL:
        conn = state["workflow_connection"]

        def get_project(self, project_id):
            return {"id": str(project_id), "enabled": True}

        def get_project_secret(self, *, project_id, secret_key):
            state["secret_project_id"] = str(project_id)
            if (
                str(project_id) == "11"
                and secret_key == "workflow_entrypoint.inventory_sync.secret"
            ):
                return "token-123"
            return ""

        def log_action(self, **kwargs):
            state["logs"].append(dict(kwargs))

    app = FastAPI()
    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: _PinnedSecretBL(),
            enqueue_task=lambda **kwargs: "job-123",
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Pinned binding secret lookup"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    assert state["secret_project_id"] == "11"


def test_serialize_workflow_entrypoint_prefers_public_base_url(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(
        mod,
        "public_base_url",
        lambda: "https://powder-skating-actors-merchandise.trycloudflare.com",
    )
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )

    payload = mod._serialize_workflow_entrypoint(
        {
            "entrypoint_id": "ep-1",
            "workflow_id": "wf.inventory.sync",
            "entry_kind": "webhook",
            "entry_key": "inventory_sync",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "response_mode": "accepted",
            "input_mapping_jsonb": {},
        }
    )

    assert payload["public_url"].startswith(
        "https://powder-skating-actors-merchandise.trycloudflare.com/webhook/workflows/"
    )
    assert payload["public_url_source"] == "platform_public_base_url"


def _entrypoint_serializer_row():
    return {
        "entrypoint_id": "ep-1",
        "workflow_id": "wf.inventory.sync",
        "entry_kind": "webhook",
        "entry_key": "inventory_sync",
        "enabled": True,
        "auth_mode": "bearer_token",
        "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
        "source_restrictions_jsonb": {},
        "allowed_content_types_jsonb": ["application/json"],
        "max_body_bytes": 4096,
        "max_file_bytes": 4096,
        "max_files": 2,
        "idempotency_mode": "none",
        "response_mode": "accepted",
        "input_mapping_jsonb": {},
    }


def _patch_serializer_interface(monkeypatch, mod):
    monkeypatch.setattr(
        mod,
        "WorkflowInterfaceService",
        lambda: SimpleNamespace(
            interface_for_workflow_id=lambda _workflow_id: SimpleNamespace(expected_inputs=[]),
            validate_inputs=lambda interface, payload, channel="": SimpleNamespace(
                ok=True,
                errors=[],
                interface={"enabled": True, "channel": channel},
            ),
        ),
    )


def test_serialize_workflow_entrypoint_does_not_use_request_origin_fallback(monkeypatch):
    # FS-DEEP-CAND-062 negative: with no canonical public base URL configured, the
    # serialized public_url must NOT be derived from the (spoofable) request origin.
    # It is emitted as a relative path instead.
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(mod, "public_base_url", lambda: "")
    _patch_serializer_interface(monkeypatch, mod)

    hostile_request = SimpleNamespace(base_url="https://attacker.evil.example/")

    payload = mod._serialize_workflow_entrypoint(
        _entrypoint_serializer_row(),
        hostile_request,
    )

    assert "attacker.evil.example" not in payload["public_url"]
    assert payload["public_url"] == "/webhook/workflows/inventory_sync"
    assert payload["public_url_source"] == "relative_path_fallback"


def test_serialize_workflow_entrypoint_uses_canonical_base_when_configured(monkeypatch):
    # FS-DEEP-CAND-062 positive: a configured canonical base URL is used even if the
    # request origin differs.
    mod = _workflow_entrypoint_test_surface()

    monkeypatch.setattr(mod, "public_base_url", lambda: "https://hooks.acme.example")
    _patch_serializer_interface(monkeypatch, mod)

    hostile_request = SimpleNamespace(base_url="https://attacker.evil.example/")

    payload = mod._serialize_workflow_entrypoint(
        _entrypoint_serializer_row(),
        hostile_request,
    )

    assert payload["public_url"] == ("https://hooks.acme.example/webhook/workflows/inventory_sync")
    assert payload["public_url_source"] == "platform_public_base_url"


def test_workflow_entrypoint_logs_delivery_meta_for_accepted_request(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch)

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory now"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 202
    logged = state["logs"][-1]
    meta = logged.get("meta") or {}
    assert meta["source_ip"] == "testclient"
    assert meta["auth_mode"] == "bearer_token"
    assert meta["auth_outcome"] == "passed"
    assert meta["content_type"] == "application/json"
    assert meta["payload_summary"]["body_keys"] == ["title"]


def test_workflow_entrypoint_logs_missing_idempotency_key_rejection(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch, idempotency_mode="header")

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory now"},
        headers={"Authorization": "Bearer token-123"},
    )

    assert response.status_code == 400
    logged = state["logs"][-1]
    assert logged["status"] == "rejected"
    assert logged["meta"]["http_status"] == 400
    assert logged["payload"]["reason"] == "idempotency_key_required"


def test_workflow_entrypoint_logs_in_progress_idempotency_rejection(monkeypatch):
    def _claim(_bl, *, entry_key, idempotency_key, request_id, request_fingerprint=None):
        return {"claimed": False, "job_id": "", "request_id": request_id}

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory now"},
        headers={
            "Authorization": "Bearer token-123",
            "Idempotency-Key": "idem-77",
        },
    )

    assert response.status_code == 409
    logged = state["logs"][-1]
    assert logged["status"] == "rejected"
    assert logged["meta"]["http_status"] == 409
    assert logged["payload"]["reason"] == "idempotency_in_progress"
    assert state["events"][-1]["payload"]["no_job_created"] is True


def test_workflow_entrypoint_duplicate_idempotency_attempt_records_event_without_enqueue(
    monkeypatch,
):
    def _claim(_bl, *, entry_key, idempotency_key, request_id, request_fingerprint=None):
        return {"claimed": False, "job_id": "job-77", "request_id": request_id}

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        json={"title": "Sync inventory now"},
        headers={
            "Authorization": "Bearer token-123",
            "Idempotency-Key": "idem-77",
        },
    )

    assert response.status_code == 200
    assert state["enqueued"] == 0
    assert state["events"][-1]["event_type"] == "workflow.ingress.duplicate"
    assert state["events"][-1]["payload"]["existing_job_id"] == "job-77"


def test_serialize_workflow_entrypoint_readiness_uses_mapping_coverage(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    class _Service:
        def interface_for_workflow_id(self, _workflow_id):
            return SimpleNamespace(
                expected_inputs=[SimpleNamespace(name="email", required=True)],
            )

        def validate_inputs(self, interface, payload, channel=""):
            return SimpleNamespace(
                ok=False,
                errors=[
                    {
                        "code": "workflow.input.required",
                        "message": "Workflow input 'email' is required",
                        "path": "inputs.email",
                        "channel": channel,
                    }
                ],
                interface={"enabled": True},
            )

    from core.entrypoints.api import workflow_entrypoints_registry as registry_mod

    monkeypatch.setattr(registry_mod, "WorkflowInterfaceService", _Service)

    payload = mod._serialize_workflow_entrypoint(
        {
            "entrypoint_id": "ep-1",
            "workflow_id": "wf.inventory.sync",
            "entry_kind": "webhook",
            "entry_key": "inventory_sync",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["application/json"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "response_mode": "accepted",
            "input_mapping_jsonb": {"email": "body.email"},
        }
    )

    assert payload["readiness"]["status"] == "ready"
    assert payload["readiness"]["errors"] == []


def test_serialize_workflow_entrypoint_readiness_rejects_text_plain_automap_for_required_inputs(
    monkeypatch,
):
    mod = _workflow_entrypoint_test_surface()

    class _Service:
        def interface_for_workflow_id(self, _workflow_id):
            return SimpleNamespace(
                expected_inputs=[SimpleNamespace(name="email", required=True)],
            )

        def validate_inputs(self, interface, payload, channel=""):
            return SimpleNamespace(
                ok=False,
                errors=[
                    {
                        "code": "workflow.input.required",
                        "message": "Workflow input 'email' is required",
                        "path": "inputs.email",
                        "channel": channel,
                    }
                ],
                interface={"enabled": True},
            )

    from core.entrypoints.api import workflow_entrypoints_registry as registry_mod

    monkeypatch.setattr(registry_mod, "WorkflowInterfaceService", _Service)

    payload = mod._serialize_workflow_entrypoint(
        {
            "entrypoint_id": "ep-1",
            "workflow_id": "wf.inventory.sync",
            "entry_kind": "webhook",
            "entry_key": "inventory_sync",
            "enabled": True,
            "auth_mode": "bearer_token",
            "auth_config_jsonb": {"secret_ref": "workflow_entrypoint.inventory_sync.secret"},
            "source_restrictions_jsonb": {},
            "allowed_content_types_jsonb": ["text/plain"],
            "max_body_bytes": 4096,
            "max_file_bytes": 4096,
            "max_files": 2,
            "idempotency_mode": "none",
            "response_mode": "accepted",
            "input_mapping_jsonb": {},
        }
    )

    assert payload["readiness"]["status"] == "invalid"
    assert payload["readiness"]["errors"][0]["code"] == "workflow.input.mapping_required"
    assert "explicit mapping" in payload["readiness"]["errors"][0]["message"]


def test_serialize_delivery_exposes_debug_meta():
    from core.application.management.query_models_workflows import WorkflowWebhookDeliveryView

    mod = _workflow_entrypoint_test_surface()

    payload = mod._serialize_delivery(
        {
            "id": "delivery-1",
            "task_id": "webhook:demo:req-1",
            "task_summary": "Webhook entrypoint demo",
            "status": "rejected",
            "timestamp": "2026-05-11T10:00:00Z",
            "payload": {
                "request_id": "req-1",
                "idempotency_key_digest": "digest-1",
                "reason": "workflow_input_validation_failed",
            },
            "result": {"job_id": "job-1", "deduplicated": False},
            "error": "Workflow input validation failed",
            "meta": {
                "source_ip": "203.0.113.10",
                "auth_mode": "basic_auth",
                "auth_outcome": "passed",
                "auth_category": "basic_auth",
                "http_status": 400,
                "content_type": "application/json",
                "response_mode": "accepted",
                "validation_errors": [{"code": "workflow.input.required"}],
                "payload_summary": {"body_keys": ["email"], "file_count": 0},
            },
        }
    )

    assert payload["source_ip"] == "203.0.113.10"
    assert payload["auth_mode"] == "basic_auth"
    assert payload["auth_outcome"] == "passed"
    assert payload["auth_category"] == "basic_auth"
    assert payload["http_status"] == 400
    assert payload["content_type"] == "application/json"
    assert payload["response_mode"] == "accepted"
    assert payload["validation_errors"] == [{"code": "workflow.input.required"}]
    assert payload["payload_summary"] == {
        "body_keys": ["email"],
        "file_count": 0,
    }
    assert payload["idempotency_key_digest"] == "digest-1"
    assert "idempotency_key" not in payload
    response = WorkflowWebhookDeliveryView.model_validate(payload).model_dump()
    assert response["idempotency_key_digest"] == "digest-1"
    assert "idempotency_key" not in response


class _RecordingCursor:
    description = [  # noqa: RUF012
        ("entry_key",),
        ("idempotency_key",),
        ("request_id",),
        ("status",),
        ("job_id",),
        ("updated_at",),
    ]

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((str(sql), tuple(params or ())))

    def fetchone(self):
        if self.rows:
            return self.rows.pop(0)
        return None


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_delete_workflow_entrypoint_does_not_delete_shared_idempotency_records(monkeypatch):
    from core.application.workflows import workflow_entrypoint_repository as repo

    cursor = _RecordingCursor()
    cursor.rowcount = 1
    connection = _RecordingConnection(cursor)
    monkeypatch.setattr(repo, "transaction_connection", lambda conn: nullcontext(conn))

    assert repo.delete_workflow_entrypoint(SimpleNamespace(conn=connection), entry_key="hook-1")

    assert len(cursor.executed) == 1
    assert "DELETE FROM workflow_entrypoints" in cursor.executed[0][0]
    assert cursor.executed[0][1] == ("hook-1",)


def test_request_idempotency_schema_is_canonical_and_entrypoint_independent():
    root = Path(__file__).resolve().parents[2]
    table_schema = (root / "db/schema/001_schema_part_05.sql").read_text()
    foreign_keys = (root / "db/schema/001_schema_part_11.sql").read_text()

    assert "request_fingerprint text" in table_schema
    assert "replay_response_jsonb jsonb" in table_schema
    assert "result_expires_at timestamp with time zone" in table_schema
    assert "tombstone_expires_at timestamp with time zone" in table_schema
    assert "CREATE TABLE public.request_idempotency" in table_schema
    assert "scope_key text NOT NULL" in table_schema
    assert "key_digest text NOT NULL" in table_schema
    assert "request_fingerprint text NOT NULL" in table_schema
    assert "workflow_entrypoint_idempotency" not in table_schema
    assert "workflow_entrypoint_idempotency" not in foreign_keys


def test_workflow_entrypoint_fails_closed_when_idempotency_table_missing(monkeypatch):
    def _claim(_bl, *, entry_key, idempotency_key, request_id, request_fingerprint=None):
        raise RuntimeError("relation request_idempotency does not exist")

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        headers={"Idempotency-Key": "idem-1", "Authorization": "Bearer token-123"},
        json={"title": "x"},
    )

    assert response.status_code == 503
    assert "reservation storage is unavailable" in response.json()["error"]
    assert state["enqueued"] == 0


def test_workflow_entrypoint_fails_closed_when_entrypoint_table_missing(monkeypatch):
    mod = _workflow_entrypoint_test_surface()

    def _raise_missing(_bl, _entry_key):
        raise RuntimeError("relation workflow_entrypoints does not exist")

    monkeypatch.setattr(mod, "_load_workflow_entrypoint", _raise_missing)

    app = FastAPI()
    app.include_router(
        mod.create_workflow_entrypoints_router(
            get_logger_only=lambda: object(),
            enqueue_task=lambda **_kwargs: 1,
            artifact_plane=None,
            logger=logging.getLogger("tests.workflow_entrypoints_route"),
            err_database_not_configured="database_unavailable",
        )
    )

    response = TestClient(app).post("/webhook/workflows/inventory_sync", json={"title": "x"})

    assert response.status_code == 503
    assert response.json()["error"] == "Workflow entrypoint storage is unavailable"


def test_workflow_entrypoint_fails_closed_when_idempotency_claim_errors(monkeypatch):
    def _claim(_bl, *, entry_key, idempotency_key, request_id, request_fingerprint=None):
        raise RuntimeError("temporary db failure")

    client, state = _build_entrypoint_client(
        monkeypatch,
        idempotency_mode="header",
        claim_impl=_claim,
    )

    response = client.post(
        "/webhook/workflows/inventory_sync",
        headers={"Idempotency-Key": "idem-2", "Authorization": "Bearer token-123"},
        json={"title": "x"},
    )

    assert response.status_code == 503
    assert "reservation failed" in response.json()["error"]
    assert state["enqueued"] == 0


def test_generate_entry_key_is_opaque_and_unique():
    keys = {_generate_entry_key() for _ in range(16)}

    assert len(keys) == 16
    assert all(len(item) >= 24 for item in keys)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in keys)


def test_trigger_catalog_excludes_callback_only_extensions(monkeypatch):
    from core.application.workflows import workflow_trigger_catalog as mod

    class _BL:
        conn = object()

        def list_extensions(self, enabled_only=True):
            assert enabled_only is True
            return [
                {
                    "extension_key": "fixture.event-provider",
                    "kind": "provider",
                    "compiled_manifest": {
                        "callbacks": [
                            {
                                "callback_id": "updates",
                                "provider_key": "fixture_events",
                            }
                        ]
                    },
                },
                {
                    "extension_key": "sample_tasks",
                    "kind": "provider",
                    "compiled_manifest": {
                        "workflow_triggers": [
                            {
                                "trigger_type_id": "task-created",
                                "provider_key": "sample_tasks",
                                "display_name": "Sample Tasks task created",
                                "config_schema": {"required": ["space_id"]},
                                "ui_schema": {
                                    "fields": [
                                        {
                                            "path": "space_id",
                                            "label": "Space",
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                },
            ]

    from core.application.workflows import workflow_trigger_catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "normalize_extension_row_for_runtime", lambda row: row)

    catalog = mod.list_workflow_trigger_catalog(_BL())

    assert catalog[0]["catalog_id"] == "incoming-webhook-post"
    assert any(item.get("provider_key") == "sample_tasks" for item in catalog)
    assert not any(item.get("provider_key") == "fixture_events" for item in catalog)


def test_projected_workflow_dependency_marker_blocks_new_runs(monkeypatch) -> None:
    from core.entrypoints.admin import workflows_projection

    monkeypatch.setattr(
        workflows_projection,
        "_active_extension_ids_for_projection",
        lambda _db, *, account_id: set(),
        raising=False,
    )
    monkeypatch.setattr(
        "core.application.extensions.install_availability_service.InstallAvailabilityService.get_install_availability",
        lambda *_args, **_kwargs: {
            "availability_status": "available",
            "new_runs_blocked": False,
            "visible_in_catalog": True,
        },
    )

    payload = workflows_projection._annotate_extension_workflow_availability(
        object(),
        {
            "workflow_id": "consumer_workflow",
            "source_install_id": "consumer-install",
            "enabled": True,
            "dependencies": {"extensions": [{"extension_id": "acme.provider", "required": True}]},
            "extension_dependency_availability": {
                "missing_extensions": ["acme.provider"],
                "new_runs_blocked": True,
            },
        },
    )

    assert payload["availability_status"] == "unavailable"
    assert payload["new_runs_blocked"] is True
    assert payload["enabled"] is False


def test_projected_workflow_dependency_marker_recovers_after_reinstall(monkeypatch) -> None:
    from core.entrypoints.admin import workflows_projection

    monkeypatch.setattr(
        workflows_projection,
        "_active_extension_ids_for_projection",
        lambda _db, *, account_id: {"acme.provider"},
        raising=False,
    )
    monkeypatch.setattr(
        workflows_projection,
        "_active_extension_versions_for_projection",
        lambda _db, *, account_id: None,
        raising=False,
    )
    monkeypatch.setattr(
        "core.application.extensions.install_availability_service.InstallAvailabilityService.get_install_availability",
        lambda *_args, **_kwargs: {
            "availability_status": "available",
            "new_runs_blocked": False,
            "visible_in_catalog": True,
        },
    )

    payload = workflows_projection._annotate_extension_workflow_availability(
        object(),
        {
            "workflow_id": "consumer_workflow",
            "source_install_id": "consumer-install",
            "enabled": True,
            "dependencies": {"extensions": [{"extension_id": "acme.provider", "required": True}]},
            "extension_dependency_availability": {
                "availability_status": "unavailable",
                "missing_extensions": ["acme.provider"],
                "new_runs_blocked": True,
            },
        },
    )

    assert payload["availability_status"] == "available"
    assert payload["new_runs_blocked"] is False
    assert payload["enabled"] is True


def test_removed_internal_mcp_bridge_has_no_route_or_enqueue(monkeypatch):
    client, state = _build_entrypoint_client(monkeypatch)
    response = client.post(
        "/api/workflow-entrypoints/inventory_sync/invoke",
        json={"payload": {"title": "retired bridge"}},
    )
    assert response.status_code == 404
    assert state["enqueued"] == 0


@pytest.fixture(autouse=True)
def _unit_trigger_mutation_owner_fences(monkeypatch):
    # Route behavior fixtures inject repository state; physical fences use PG tests.
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "core.application.workflows.workflow_webhook_creation_service.workflow_trigger_transaction",
        lambda *_args, **_kwargs: nullcontext(MagicMock()),
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_webhook_creation_service.guard_project_credential_versions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_webhook_creation_service.lock_entrypoint",
        lambda _cursor, *, entry_key, workflow_id: (
            _workflow_entrypoint_test_surface()._load_workflow_entrypoint(None, entry_key)
        ),
    )
    monkeypatch.setattr(
        "core.application.workflows.workflow_provider_trigger_service.workflow_trigger_transaction",
        lambda *_args, **_kwargs: nullcontext(),
    )
