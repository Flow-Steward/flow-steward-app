"""Tests for ExtensionCommandService and ExtensionQueryService — extension productization."""

from __future__ import annotations

import asyncio
import copy
import json
import urllib.error
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.command_service import ExtensionCommandService
from core.application.extensions.query_service import ExtensionQueryService
from core.application.extensions.uninstall_models import UninstallContext, UninstallTarget
from core.application.management.command_models import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorImportUrlInput,
    ExtensionGeneratedJobCreateInput,
    ExtensionReleaseInstallInput,
    ExtensionRuntimeTokenInput,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router

# FS-DEEP-CAND-042/043/044: extension build / descriptor-import / bootstrap /
# runtime-token issuance now require the deployment lifecycle / runtime admin
# gate (admin role + default account). Lifecycle-seeding helpers use this scope.
_ADMIN_SCOPE = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="admin", principal_type="admin")
_OTHER_ADMIN_SCOPE = ScopeContext(
    account_id="00000000-0000-0000-0000-000000000202",
    actor_id="admin",
    principal_type="admin",
)


def _make_install_store():
    from core.application.extensions.install import ExtensionInstallStore

    return ExtensionInstallStore()


def _make_runtime_store():
    from core.application.extensions.runtime_store import ExtensionRuntimeStore

    return ExtensionRuntimeStore()


def _stub_unreachable_remote_runtime(monkeypatch) -> list[str]:
    """Refuse every remote-runtime request at once instead of opening a socket.

    A ``remote_https`` install runs the runtime handshake and health check. With
    the runtime host pinned to a public test address, the real transport waits
    out two 15 s connect timeouts where egress is blocked (and reaches a real
    host where it is not). Returns the URLs the host tried to reach.
    """
    attempted: list[str] = []

    def _refuse(request, *, timeout_seconds, purpose, require_private=False):
        del timeout_seconds, purpose, require_private
        attempted.append(request.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr("core.application.extensions.url_safety.open_pinned_url", _refuse)
    return attempted


def test_uninstall_target_uses_existing_install_when_legacy_bundle_is_missing():
    from core.application.extensions.binding_service import ExtensionBindingService

    install_store = _make_install_store()
    runtime_store = _make_runtime_store()
    install_store.install(
        extension_id="acme.missing-bundle",
        version="1.0.0",
        account_id=DEFAULT_ACCOUNT_ID,
        install_id="install-missing-bundle",
        descriptor_source="file",
    )
    bl = MagicMock()
    bl.get_extension.return_value = None
    service = ExtensionBindingService(
        get_logger=lambda: bl,
        resolve_extension_runtime_record=lambda extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *args, **kwargs: "extension",
        extension_operation_failure_status=lambda invocation: 502,
        get_operation_store=lambda: bl,
        get_extension_install_store=lambda: install_store,
        get_extension_runtime_store=lambda: runtime_store,
    )
    context = UninstallContext(
        extension_id="acme.missing-bundle",
        scope=_ADMIN_SCOPE,
        resolved_scope=_ADMIN_SCOPE,
        plan_revision="revision",
        account_id=DEFAULT_ACCOUNT_ID,
        bl=bl,
        runtime_store=runtime_store,
        confirmed_plan={"install_id": "install-missing-bundle", "release_id": ""},
    )

    target = service._uninstall_service._resolve_uninstall_target(context)

    assert isinstance(target, UninstallTarget)
    assert target.install_id == "install-missing-bundle"
    assert target.extension["extension_key"] == "acme.missing-bundle"


def test_uninstall_target_uses_install_descriptor_source_for_existing_bundle():
    from core.application.extensions.binding_service import ExtensionBindingService

    install_store = _make_install_store()
    runtime_store = _make_runtime_store()
    install_store.install(
        extension_id="acme.archive-bundle",
        version="1.0.0",
        account_id=DEFAULT_ACCOUNT_ID,
        install_id="install-archive-bundle",
        descriptor_source="file",
    )
    bl = MagicMock()
    bl.get_extension.return_value = {
        "extension_key": "acme.archive-bundle",
        "bundle_root": "/app/extensions/acme_archive_bundle",
        "descriptor_source": "extension_scan",
    }
    service = ExtensionBindingService(
        get_logger=lambda: bl,
        resolve_extension_runtime_record=lambda extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *args, **kwargs: "extension",
        extension_operation_failure_status=lambda invocation: 502,
        get_operation_store=lambda: bl,
        get_extension_install_store=lambda: install_store,
        get_extension_runtime_store=lambda: runtime_store,
    )
    context = UninstallContext(
        extension_id="acme.archive-bundle",
        scope=_ADMIN_SCOPE,
        resolved_scope=_ADMIN_SCOPE,
        plan_revision="revision",
        account_id=DEFAULT_ACCOUNT_ID,
        bl=bl,
        runtime_store=runtime_store,
        confirmed_plan={"install_id": "install-archive-bundle", "release_id": ""},
    )

    target = service._uninstall_service._resolve_uninstall_target(context)

    assert isinstance(target, UninstallTarget)
    assert target.extension["descriptor_source"] == "file"


def _make_services(store=None, runtime_store=None, bl=None):
    store = store or _make_install_store()
    runtime_store = runtime_store or _make_runtime_store()
    bl = bl or MagicMock()
    return (
        ExtensionCommandService(
            get_logger=lambda: bl,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        ),
        ExtensionQueryService(
            get_logger=lambda: bl,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        ),
        store,
        runtime_store,
        bl,
    )


def _make_cursor_logger(rows: list[dict] | None = None) -> MagicMock:
    bl = MagicMock()
    cursor_inner = MagicMock()
    cursor_inner.fetchall.return_value = list(rows or [])
    cursor_inner.fetchone.return_value = None
    cursor_ctx = MagicMock()
    cursor_ctx.__enter__ = MagicMock(return_value=cursor_inner)
    cursor_ctx.__exit__ = MagicMock(return_value=False)
    bl.cursor = MagicMock(return_value=cursor_ctx)
    return bl


def _service_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    return JSONResponse(
        {"error": getattr(exc, "message", str(exc))},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _production_service_error_response(exc, request=None) -> JSONResponse:
    from core.infrastructure.runtime.serve_request_runtime import (
        error_payload,
        service_error_response,
    )

    return service_error_response(
        exc,
        request,
        error_payload=lambda **kwargs: error_payload(
            **kwargs,
            err_request_failed="Request failed",
            default_correlation_id_fn=lambda: "test-correlation-id",
        ),
        request_correlation_id=lambda *_args, **_kwargs: "test-correlation-id",
        error_code_for_status=lambda status: f"http_{status}",
        err_request_failed="Request failed",
    )


def _api_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def _make_extension_api_client(
    store=None,
    runtime_store=None,
    bl=None,
    extension_binding_service=None,
    query_service=None,
    service_error_handler=_service_error_response,
) -> tuple[TestClient, Any, Any, Any]:
    store = store or _make_install_store()
    runtime_store = runtime_store or _make_runtime_store()
    bl = bl or _make_cursor_logger()
    query_service = query_service or ExtensionQueryService(
        get_logger=lambda: bl,
        get_install_store=lambda: store,
        get_runtime_store=lambda: runtime_store,
    )
    app = FastAPI()

    @app.middleware("http")
    async def _test_auth_context(request: Request, call_next):
        request.state.auth_user = {
            "account_id": str(
                request.headers.get("x-test-account-id") or DEFAULT_ACCOUNT_ID
            ).strip(),
            "roles": [str(request.headers.get("x-test-role") or "admin").strip().lower()],
        }
        return await call_next(request)

    def _request_scope(request: Request, **kwargs):
        return ScopeContext(
            account_id=str(request.headers.get("x-test-account-id") or DEFAULT_ACCOUNT_ID).strip(),
            actor_id=str(request.headers.get("x-test-actor") or "api").strip(),
            principal_type=str(request.headers.get("x-test-role") or "admin").strip().lower(),
            project_id=kwargs.get("project_id"),
        )

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: bl,
            get_extension_install_store=lambda: store,
            get_extension_runtime_store=lambda: runtime_store,
            extension_query_service=lambda: query_service,
            extension_binding_service=(
                (lambda: extension_binding_service)
                if extension_binding_service is not None
                else None
            ),
            request_scope=_request_scope,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            service_error_response=service_error_handler,
            api_error_response=_api_error_response,
            err_not_found="Not found",
        )
    )
    return TestClient(app, raise_server_exceptions=False), store, runtime_store, bl


def _make_complete_extension_api_client(
    store=None,
    runtime_store=None,
    bl=None,
    query_service=None,
    extension_binding_service=None,
    service_error_handler=_service_error_response,
) -> tuple[TestClient, Any, Any, Any]:
    store = store or _make_install_store()
    runtime_store = runtime_store or _make_runtime_store()
    bl = bl or _make_cursor_logger()
    query_service = query_service or ExtensionQueryService(
        get_logger=lambda: bl,
        get_install_store=lambda: store,
        get_runtime_store=lambda: runtime_store,
    )
    client, store, runtime_store, bl = _make_extension_api_client(
        store=store,
        runtime_store=runtime_store,
        bl=bl,
        query_service=query_service,
        service_error_handler=service_error_handler,
    )
    client.app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: MagicMock(),
            event_command_service=lambda: MagicMock(),
            job_query_service=lambda: MagicMock(),
            extension_query_service=lambda: query_service,
            extension_binding_service=lambda: extension_binding_service or MagicMock(),
            tool_execution_service=lambda: MagicMock(),
            command_dispatch_service=lambda: MagicMock(),
            request_scope=lambda request, **kwargs: ScopeContext(
                account_id=str(
                    request.headers.get("x-test-account-id") or DEFAULT_ACCOUNT_ID
                ).strip(),
                actor_id="api",
                principal_type="admin",
                project_id=kwargs.get("project_id"),
            ),
            service_error_response=service_error_handler,
            api_error_response=_api_error_response,
        )
    )
    return client, store, runtime_store, bl


def test_extension_action_route_serializes_only_safe_success_and_failure_payloads() -> None:
    from core.application.management.command_models import ExtensionActionResultView
    from core.application.management.errors import ServiceError

    safe_action = {
        "status": "succeeded",
        "detail": "",
        "response": {"ok": True, "result": {"ticket": "safe-ticket"}},
        "errors": [],
    }
    blocked_action = {
        "status": "failed",
        "detail": "extension_secret_exposure_blocked",
        "response": {
            "ok": False,
            "error": "The extension returned protected credential material. Contact the extension author.",
            "error_code": "extension_secret_exposure_blocked",
        },
        "errors": [{"code": "extension_secret_exposure_blocked"}],
    }
    service = MagicMock()
    service.execute_action.side_effect = [
        ExtensionActionResultView(
            extension_id="fixture.safe",
            action_id="inspect",
            action=safe_action,
        ),
        ServiceError(
            "Extension action failed",
            status_code=400,
            payload={
                "extension_id": "fixture.safe",
                "action_id": "inspect",
                "action": blocked_action,
            },
        ),
    ]
    client, _, _, _ = _make_complete_extension_api_client(
        extension_binding_service=service,
        service_error_handler=_production_service_error_response,
    )

    success = client.post("/api/extensions/fixture.safe/action/inspect", json={})
    failure = client.post("/api/extensions/fixture.safe/action/inspect", json={})

    assert success.status_code == 200
    assert success.json() == {
        "extension_id": "fixture.safe",
        "action_id": "inspect",
        "action": safe_action,
    }
    assert failure.status_code == 400
    failure_payload = failure.json()
    assert failure_payload["action"] == blocked_action
    serialized = json.dumps({"success": success.json(), "failure": failure_payload})
    for forbidden in (
        "runtime-token",
        "command",
        "bundle_root",
        "stdout",
        "stderr",
        "audit_context",
    ):
        assert forbidden not in serialized


def test_enable_transport_preserves_structured_browser_authority_precondition() -> None:
    from core.application.management.errors import ServicePreconditionError

    client, _store, _runtime_store, _logger = _make_extension_api_client(
        service_error_handler=_production_service_error_response
    )
    expected = {
        "error": "browser_authority_approval_required",
        "impact_hash": "a" * 64,
        "authority_contract_version": "extension_browser_authority_v1",
        "extension_id": "acme.picker",
        "remote_script_urls": ["https://sdk.example.test/picker.js"],
        "remote_origins": ["https://sdk.example.test"],
        "local_asset_refs": ["assets/picker.js"],
    }
    with patch(
        "core.application.extensions.command_service.ExtensionCommandService"
    ) as service_cls:
        service_cls.return_value.enable_extension.side_effect = ServicePreconditionError(
            "Privileged approval is required",
            payload=expected,
        )
        response = client.post("/api/extensions/acme.picker/enable", json={})

    assert response.status_code == 412
    payload = response.json()
    assert payload["error"] == expected["error"]
    for key, value in expected.items():
        if key != "error":
            assert payload[key] == value


def test_enable_transport_rejects_viewer_scope() -> None:
    client, _store, _runtime_store, _logger = _make_extension_api_client(
        service_error_handler=_production_service_error_response
    )

    response = client.post(
        "/api/extensions/acme.picker/enable",
        headers={"x-test-role": "viewer"},
        json={},
    )

    assert response.status_code == 403


class TestExtensionInstall:
    def test_install_creates_record(self):
        _cmd_svc, query_svc, store, _, _ = _make_services()
        result = store.install(
            extension_id="my-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )
        assert result.state == "active"
        assert result.extension_id == "my-extension"

        # Verify it shows up in list
        extensions = query_svc.list_extensions(scope=_ADMIN_SCOPE)
        assert extensions.total == 1
        assert extensions.extensions[0].extension_id == "my-extension"

    def test_list_state_all_is_unfiltered(self):
        client, store, _runtime_store, _bl = _make_extension_api_client()
        store.install(
            extension_id="active-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        store.install(
            extension_id="disabled-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        store.disable(extension_id="disabled-extension", account_id=DEFAULT_ACCOUNT_ID)

        response = client.get("/api/extensions?state=all")

        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_project_extension_state_api_lists_project_state(self):
        client, store, _runtime_store, bl = _make_extension_api_client()
        install = store.install(
            extension_id="example.host-fixture",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        project_id = "00000000-0000-0000-0000-000000000007"
        bl.get_project.return_value = {
            "id": project_id,
            "name": "Project",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        state_row = {
            "state_id": "state_1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": project_id,
            "extension_id": "example.host-fixture",
            "namespace": "settings",
            "state_key": "setting:primary",
            "value_jsonb": {"value": "Hello"},
            "revision": 1,
        }

        with patch(
            "core.application.extensions.extension_project_state.list_state",
            return_value=[state_row],
        ) as list_state:
            response = client.get(
                f"/api/projects/{project_id}/extension-state/example.host-fixture?namespace=settings"
            )

        assert response.status_code == 200
        body = response.json()
        assert body["state"][0]["state_key"] == "setting:primary"
        list_state.assert_called_once()
        assert list_state.call_args.kwargs["account_id"] == DEFAULT_ACCOUNT_ID
        assert list_state.call_args.kwargs["extension_install_id"] == install.install_id
        assert list_state.call_args.kwargs["namespace"] == "settings"

    def test_project_extension_state_api_puts_generic_state(self):
        client, store, _runtime_store, bl = _make_extension_api_client()
        install = store.install(
            extension_id="example.host-fixture",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        project_id = "00000000-0000-0000-0000-000000000007"
        bl.get_project.return_value = {
            "id": project_id,
            "name": "Project",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        state_row = {
            "state_id": "state_1",
            "state_key": "setting:primary",
            "value_jsonb": {"value": "Hello {{user.name}}"},
            "revision": 1,
        }

        with patch(
            "core.application.extensions.extension_project_state.put_state",
            return_value=state_row,
        ) as put_state:
            response = client.put(
                f"/api/projects/{project_id}/extension-state/example.host-fixture/settings/setting%3Aprimary",
                json={"value_jsonb": {"value": "Hello {{user.name}}"}},
            )

        assert response.status_code == 200
        assert response.json()["state_key"] == "setting:primary"
        put_state.assert_called_once()
        assert put_state.call_args.kwargs["extension_id"] == "example.host-fixture"
        assert put_state.call_args.kwargs["extension_install_id"] == install.install_id
        assert put_state.call_args.kwargs["namespace"] == "settings"
        assert put_state.call_args.kwargs["state_key"] == "setting:primary"
        assert put_state.call_args.kwargs["value_jsonb"] == {"value": "Hello {{user.name}}"}

    def test_project_extension_state_api_rejects_connection_from_other_extension(self):
        client, _store, _runtime_store, bl = _make_extension_api_client()
        project_id = "00000000-0000-0000-0000-000000000007"
        bl.get_project.return_value = {
            "id": project_id,
            "name": "Project",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "connection_id": "conn_other",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": project_id,
            "extension_id": "acme.notification-provider",
            "enabled": True,
        }

        with patch("core.application.extensions.extension_project_state.list_state") as list_state:
            response = client.get(
                f"/api/projects/{project_id}/extension-state/example.host-fixture"
                "?namespace=settings&connection_id=conn_other"
            )

        assert response.status_code == 400
        assert "does not belong" in response.text
        list_state.assert_not_called()

    def test_project_extension_state_resolves_install_for_owned_legacy_connection(self):
        client, store, _runtime_store, bl = _make_extension_api_client()
        installed = store.install(
            extension_id="example.host-fixture",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        project_id = "00000000-0000-0000-0000-000000000007"
        bl.get_project.return_value = {
            "id": project_id,
            "name": "Project",
            "account_id": DEFAULT_ACCOUNT_ID,
        }
        bl.get_project_connection.return_value = {
            "connection_id": "conn_owned",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": project_id,
            "extension_id": "example.host-fixture",
            "extension_install_id": None,
            "enabled": True,
        }
        state_row = {
            "state_id": "state_1",
            "state_key": "template:welcome",
            "value_jsonb": {"subject": "Welcome", "body_text": "Hello"},
            "revision": 1,
        }

        with patch(
            "core.application.extensions.extension_project_state.put_state",
            return_value=state_row,
        ) as put_state:
            response = client.put(
                f"/api/projects/{project_id}/extension-state/example.host-fixture/"
                "templates/template%3Awelcome",
                json={
                    "connection_id": "conn_owned",
                    "value_jsonb": {"subject": "Welcome", "body_text": "Hello"},
                },
            )

        assert response.status_code == 200
        assert put_state.call_args.kwargs["extension_install_id"] == installed.install_id
        assert put_state.call_args.kwargs["connection_id"] == "conn_owned"

    def test_uninstall_removes_record(self):
        cmd_svc, query_svc, store, _, _ = _make_services()
        store.install(
            extension_id="my-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )
        result = cmd_svc.uninstall_extension("my-extension", scope=_ADMIN_SCOPE)
        assert result.status == "uninstalled"

        extensions = query_svc.list_extensions(scope=_ADMIN_SCOPE)
        assert extensions.total == 0

    def test_api_uninstall_rejects_legacy_delete_without_plan_revision(self):
        lifecycle_service = MagicMock()
        client, _store, _runtime_store, _bl = _make_extension_api_client(
            extension_binding_service=lifecycle_service
        )

        response = client.delete("/api/extensions/vendor-extension")

        assert response.status_code == 405
        lifecycle_service.uninstall_extension.assert_not_called()

    def test_api_uninstall_plan_previews_without_mutation(self):
        lifecycle_service = MagicMock()
        lifecycle_service.preview_extension_uninstall.return_value = {
            "extension_id": "acme.catalog-provider",
            "install_id": "pi_catalog",
            "release_id": "pr_catalog",
            "revision": "revision-1",
            "remove": [{"kind": "dataset", "id": "pi_catalog", "count": 1}],
            "affected": [],
        }
        client, _store, _runtime_store, _bl = _make_extension_api_client(
            extension_binding_service=lifecycle_service
        )

        response = client.get("/api/extensions/acme.catalog-provider/uninstall-plan")

        assert response.status_code == 200
        assert response.json()["revision"] == "revision-1"
        lifecycle_service.preview_extension_uninstall.assert_called_once()
        lifecycle_service.uninstall_extension.assert_not_called()

    def test_api_uninstall_confirm_forwards_plan_revision(self):
        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "acme.catalog-provider",
            "state": "uninstalled",
            "removed_data": ["registry_projections"],
        }
        lifecycle_service = MagicMock()
        lifecycle_service.uninstall_extension.return_value = payload
        client, _store, _runtime_store, _bl = _make_extension_api_client(
            extension_binding_service=lifecycle_service
        )

        response = client.post(
            "/api/extensions/acme.catalog-provider/uninstall",
            json={"revision": "revision-1"},
        )

        assert response.status_code == 200
        lifecycle_service.uninstall_extension.assert_called_once()
        call = lifecycle_service.uninstall_extension.call_args
        assert call.args == ("acme.catalog-provider",)
        assert call.kwargs["plan_revision"] == "revision-1"

    def test_uninstall_removes_orphaned_releases_for_account(self, tmp_path):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        cmd_svc.uninstall_extension(imported.extension_id, scope=_ADMIN_SCOPE)

        assert runtime_store.get_release(imported.release_id) is None

    def test_install_required_dependency_blocks_when_missing(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path,
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [
                    {
                        "extension_id": "acme.notification-provider",
                        "min_version": "1.0.0",
                        "required": True,
                    }
                ]
            },
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_725_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="Required extension dependency"):
            cmd_svc.install_release(_raises_input_725_1, scope=_ADMIN_SCOPE)

    def test_install_required_dependency_allows_compatible_active_install(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.notification-provider",
            release_version="1.2.0",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [
                    {
                        "extension_id": "acme.notification-provider",
                        "min_version": "1.0.0",
                        "max_version_exclusive": "2.0.0",
                        "required": True,
                    }
                ]
            },
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )

        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=depender.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        assert installed.extension_id == "acme.depender"

    def test_install_dependency_version_mismatch_blocks(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.notification-provider",
            release_version="1.0.0",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [
                    {
                        "extension_id": "acme.notification-provider",
                        "min_version": "1.1.0",
                        "required": True,
                    }
                ]
            },
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_809_1 = ExtensionReleaseInstallInput(
            release_id=depender.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="below minimum"):
            cmd_svc.install_release(_raises_input_809_1, scope=_ADMIN_SCOPE)

    def test_install_optional_dependency_warns_when_missing(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path,
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [{"extension_id": "acme.notification-provider", "required": False}]
            },
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )

        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        assert installed.dependency_warnings[0]["extension_id"] == "acme.notification-provider"

    def test_install_required_dependency_cycle_is_rejected(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.provider",
            dependencies={"extensions": [{"extension_id": "acme.depender", "required": True}]},
        )
        cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={"extensions": [{"extension_id": "acme.provider", "required": True}]},
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_860_1 = ExtensionReleaseInstallInput(
            release_id=depender.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="cycle"):
            cmd_svc.install_release(_raises_input_860_1, scope=_ADMIN_SCOPE)

    def test_disable_dependency_blocks_active_dependents(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.notification-provider",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [{"extension_id": "acme.notification-provider", "required": True}]
            },
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=depender.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        with pytest.raises(ServiceValidationError, match="required dependents"):
            cmd_svc.disable_extension("acme.notification-provider", scope=_ADMIN_SCOPE)

    def test_uninstall_dependency_blocks_active_dependents(self, tmp_path):
        cmd_svc, _, install_store, runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.provider",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        depender_root = tmp_path / "depender"
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            depender_root,
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={"extensions": [{"extension_id": "acme.provider", "required": True}]},
        )
        (depender_root / "artifacts" / "workflows" / "wf_sync.yaml").write_text(
            "\n".join(
                [
                    "workflow_id: wf_sync",
                    "dependencies:",
                    "  extensions:",
                    "  - extension_id: acme.provider",
                    "    required: true",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )
        depender_install = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=depender.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        with pytest.raises(ServiceValidationError, match="required dependents"):
            cmd_svc.uninstall_extension("acme.provider", scope=_ADMIN_SCOPE)

        assert (
            install_store.get(extension_id="acme.provider", account_id=DEFAULT_ACCOUNT_ID)
            is not None
        )
        assert (
            install_store.get(extension_id="acme.depender", account_id=DEFAULT_ACCOUNT_ID)
            is not None
        )
        depender_rows = [
            row
            for row in runtime_store.list_projections(
                account_id=DEFAULT_ACCOUNT_ID,
                only_active=False,
            )
            if row.install_id == depender_install.install_id and row.artifact_kind == "workflow"
        ]
        assert len(depender_rows) == 1
        assert "extension_dependency_availability" not in depender_rows[0].projection

    def test_enable_dependent_rechecks_required_dependencies(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.notification-provider",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.depender",
            dependencies={
                "extensions": [{"extension_id": "acme.notification-provider", "required": True}]
            },
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=depender.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        cmd_svc.disable_extension("acme.depender", scope=_ADMIN_SCOPE)
        cmd_svc.disable_extension("acme.notification-provider", scope=_ADMIN_SCOPE)

        with pytest.raises(ServiceValidationError, match="Required extension dependency"):
            cmd_svc.enable_extension("acme.depender", scope=_ADMIN_SCOPE)

    def test_enable_remote_https_install_requires_configured_runtime_url(self):
        cmd_svc, _, install_store, _runtime_store, _ = _make_services()
        install_store.install(
            extension_id="acme.remote-runtime",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            runtime_mode="remote_https",
            remote_base_url="",
        )
        install_store.disable(
            extension_id="acme.remote-runtime",
            account_id=DEFAULT_ACCOUNT_ID,
        )

        with pytest.raises(ServiceValidationError, match="remote_base_url is required"):
            cmd_svc.enable_extension("acme.remote-runtime", scope=_ADMIN_SCOPE)

        assert (
            install_store.get(
                extension_id="acme.remote-runtime",
                account_id=DEFAULT_ACCOUNT_ID,
            ).state.value
            == "disabled"
        )

    def test_enable_llm_provider_requires_manifest_connection_config(self):
        cmd_svc, _, install_store, runtime_store, _ = _make_services()
        descriptor = {
            "runtime": {
                "extension_contract_v2": {
                    "llm_provider": {
                        "schema_version": "llm_provider_extension_v1",
                        "supplier_key": "acme_remote_runtime",
                        "supplier_label": "Acme Runtime",
                        "runtime_mode": "remote_https",
                        "credential_provider_key": "acme_remote_runtime",
                        "required_provider_secrets": [],
                        "optional_provider_secrets": [],
                        "required_connection_config": ["upstream_base_url"],
                        "operations": {
                            "list_models": {"query_id": "llm.list_models"},
                            "chat": {"action_id": "llm.chat"},
                        },
                        "model_grouping": {"strategy": "endpoint"},
                        "required_capabilities_for_enablement": [
                            "chat",
                            "tools",
                            "structured_output",
                        ],
                    }
                }
            }
        }
        release = runtime_store.create_release(
            {
                "release_id": "pr_remote_runtime_required_config",
                "extension_id": "acme.remote-llm",
                "descriptor": descriptor,
            }
        )
        install_store.install(
            extension_id="acme.remote-llm",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_mode="remote_https",
            remote_base_url="https://runtime.example.com",
            config={"llm_provider_connection_config": {}},
        )
        install_store.disable(
            extension_id="acme.remote-llm",
            account_id=DEFAULT_ACCOUNT_ID,
        )

        with (
            patch(
                "core.application.extensions.url_safety.assert_safe_remote_https_url",
                return_value="https://runtime.example.com",
            ),
            pytest.raises(ServiceValidationError, match="Fill in the required setting: Base URL"),
        ):
            cmd_svc.enable_extension("acme.remote-llm", scope=_ADMIN_SCOPE)

        assert (
            install_store.get(
                extension_id="acme.remote-llm",
                account_id=DEFAULT_ACCOUNT_ID,
            ).state.value
            == "disabled"
        )

    def test_disable_global_dependency_blocks_foreign_account_dependents(self, tmp_path):
        cmd_svc, _, install_store, _runtime_store, _ = _make_services()
        provider_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "provider",
            execution_modes=["local"],
            extension_id="acme.notification-provider",
            release_version="1.2.0",
        )
        provider = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(provider_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=provider.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        install_store.set_visibility(
            extension_id="acme.notification-provider",
            account_id=DEFAULT_ACCOUNT_ID,
            visibility="global",
        )
        depender_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path / "depender",
            execution_modes=["local"],
            extension_id="acme.foreign-depender",
            dependencies={
                "extensions": [
                    {
                        "extension_id": "acme.notification-provider",
                        "min_version": "1.0.0",
                        "required": True,
                    }
                ]
            },
        )
        depender = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(depender_path)),
            scope=_OTHER_ADMIN_SCOPE,
        )
        _raises_input_1136_1 = ExtensionReleaseInstallInput(
            release_id=depender.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            cmd_svc.install_release(_raises_input_1136_1, scope=_OTHER_ADMIN_SCOPE)

        cmd_svc.disable_extension("acme.notification-provider", scope=_ADMIN_SCOPE)

    def test_uninstall_not_found_raises(self):
        cmd_svc, _, _, _, _ = _make_services()
        with pytest.raises(ServiceNotFoundError):
            cmd_svc.uninstall_extension("nonexistent", scope=_ADMIN_SCOPE)

    def test_disable_extension(self):
        cmd_svc, _, store, _, _ = _make_services()
        store.install(
            extension_id="my-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )
        result = cmd_svc.disable_extension("my-extension", scope=_ADMIN_SCOPE)
        assert result.state == "disabled"

    def test_enable_and_disable_sync_the_legacy_enabled_projection(self):
        # Regression: the runtime invocation path still gates on the legacy
        # enabled_extensions flag, so lifecycle toggles must dual-write it —
        # otherwise an "active" install keeps returning "Extension is disabled".
        store = _make_install_store()
        bl = MagicMock()
        synced: list[tuple[str, bool, bool, bool]] = []

        def _sync(
            _conn,
            extension_key,
            *,
            version=None,
            enabled=True,
            config=None,
            preserve_existing_install=False,
            lifecycle_state_authoritative=False,
        ):
            synced.append(
                (
                    extension_key,
                    enabled,
                    preserve_existing_install,
                    lifecycle_state_authoritative,
                )
            )
            return (extension_key, [])

        bl.sync_enabled_extension_record = _sync
        cmd_svc, _, _, _, _ = _make_services(store=store, bl=bl)
        store.install(
            extension_id="my-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )

        cmd_svc.enable_extension("my-extension", scope=_ADMIN_SCOPE)
        cmd_svc.disable_extension("my-extension", scope=_ADMIN_SCOPE)

        assert synced == [
            ("my-extension", True, True, True),
            ("my-extension", False, True, True),
        ]

    def test_get_extension_detail(self):
        _cmd_svc, query_svc, store, _, _ = _make_services()
        store.install(
            extension_id="my-extension",
            version="2.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
            config={"key": "val"},
        )
        detail = query_svc.get_extension("my-extension", scope=_ADMIN_SCOPE)
        assert detail.extension_id == "my-extension"
        assert detail.version == "2.0.0"

        safe_detail = query_svc.get_extension_safe(
            "my-extension",
            scope=_ADMIN_SCOPE,
        )
        assert safe_detail.dump() == {
            "active": True,
            "broken": False,
            "category": "",
            "description": "",
            "display_name": "my-extension",
            "extension_id": "my-extension",
            # The closed projection says whether the bundle ships a logo; this
            # install has no files, so it has none.
            "has_icon": False,
            "icon_url": "",
            "install_id": detail.install_id,
            "state": "active",
            "support_url": "",
            "tags": [],
            "vendor_name": "",
            "version": "2.0.0",
            "website_url": "",
        }

    def test_extension_detail_uses_safe_projection_for_non_admin_roles(self):
        client, store, _runtime_store, _bl = _make_complete_extension_api_client()
        record = store.install(
            extension_id="acme.viewer-safe",
            version="2.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
            config={"client_secret": "must-not-leak"},
        )

        for role in ("operator", "viewer"):
            response = client.get(
                "/api/extensions/acme.viewer-safe",
                headers={"x-test-role": role},
            )

            assert response.status_code == 200
            detail = response.json()
            assert detail["extension_id"] == "acme.viewer-safe"
            assert detail["install_id"] == record.install_id
            assert detail["state"] == "active"
            assert detail["active"] is True
            assert not {
                "artifacts",
                "compiled_manifest",
                "config",
                "connections",
                "descriptor_source",
                "entitlement",
                "metadata",
                "projections",
                "release_id",
                "remote_base_url",
                "validation_summary",
            }.intersection(detail)

        admin_response = client.get(
            "/api/extensions/acme.viewer-safe",
            headers={"x-test-role": "admin"},
        )
        assert admin_response.status_code == 200
        admin_detail = admin_response.json()
        assert admin_detail["config"] == {"client_secret": "must-not-leak"}
        assert "compiled_manifest" in admin_detail
        assert "metadata" in admin_detail

    def test_compiled_manifest_uses_closed_browser_projection_for_non_admin_roles(self):
        manifest = {
            "extension_id": "acme.viewer-manifest",
            "display_name": "Viewer Manifest",
            "account_scope": {"scope_type": "project"},
            "ui_manifest": {"version": "1", "pages": [{"page_id": "setup"}]},
            "oauth_providers": [
                {
                    "provider_id": "example",
                    "display_name": "Example Identity",
                    "description": "Connect an Example account.",
                    "setup_page_id": "setup",
                    "help_page_id": "help",
                    "error_help_anchors": {"invalid_client": "credentials"},
                    "credential_fields": [
                        {
                            "field_id": "client_id",
                            "value_type": "text",
                            "required": True,
                            "secret": False,
                            "label": "Application ID",
                            "help_text": "Public identifier.",
                            "placeholder": "client-id",
                            "autocomplete": "username",
                            "validation": {"min_length": 2, "max_length": 200},
                            "credential_role": "client_id",
                        }
                    ],
                    "authorization_intents": [
                        {
                            "intent_id": "connect",
                            "user_inputs": [
                                {
                                    "input_id": "email_hint",
                                    "validation": {"type": "email", "max_length": 320},
                                    "runtime_parameter": "login_hint",
                                }
                            ],
                            "runtime_parameters": {"prompt": "consent"},
                        }
                    ],
                    "authorization_endpoint": {"url": "https://identity.example.test/authorize"},
                    "token_endpoint": {
                        "url": "https://identity.example.test/token",
                        "client_auth_method": "client_secret_post",
                    },
                    "revocation_endpoint": {"url": "https://identity.example.test/revoke"},
                    "callback_id": "oauth_authorization",
                    "flow": {"type": "authorization_code", "pkce": "S256"},
                    "security_fingerprint": "must-not-reach-browser",
                    "post_connect_action_id": "inspect_identity",
                }
            ],
            "connection_types": [{"connection_type_id": "example_mail"}],
            "schemas": {"connection": {"type": "object"}},
            "action_definitions": [
                {
                    "action_id": "save",
                    "parameters": [{"name": "display_name", "type": "string"}],
                    "handler": {"command": ["python", "/private/owner/action.py"]},
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "save",
                    "inputs": [{"name": "display_name", "type": "string"}],
                    "handler_ref": "/private/owner/top-level-handler.py",
                }
            ],
            "extension_contract_v2_normalized": {
                "operation_manifest": [
                    {
                        "operation_id": "save",
                        "inputs": [{"name": "display_name", "type": "string"}],
                        "handler_ref": "/private/owner/handler.py",
                    }
                ],
            },
            "runtime": {
                "entrypoint": {"command": ["python", "/private/owner/runtime.py"]},
                "extension_contract_v2": {
                    "llm_provider": {"provider_key": "example"},
                    "operation_manifest": "/private/owner/operations.yaml",
                },
            },
            "bundle_root": "/private/owner/extensions/acme.viewer-manifest",
            "manifest_path": "/private/owner/extensions/acme.viewer-manifest/extension.yaml",
            "entrypoint": {"command": ["python", "/private/owner/runtime.py"]},
            "healthcheck": {"command": ["python", "/private/owner/health.py"]},
            "compile_errors": [{"file_path": "/private/owner/extension.yaml"}],
            "compile_warnings": [{"file_path": "/private/owner/warning.yaml"}],
        }
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                {"extension_key": extension_id, "compiled_manifest": manifest},
                None,
            ),
            build_extension_compiled_manifest=lambda _runtime: manifest,
        )
        client, _, _, _ = _make_complete_extension_api_client(query_service=service)

        for role in ("operator", "viewer"):
            response = client.get(
                "/api/extensions/acme.viewer-manifest/compiled-manifest",
                headers={"x-test-role": role},
            )
            assert response.status_code == 200
            projected = response.json()
            assert projected["ui_manifest"] == manifest["ui_manifest"]
            assert projected["oauth_providers"] == [
                {
                    "provider_id": "example",
                    "display_name": "Example Identity",
                    "description": "Connect an Example account.",
                    "setup_page_id": "setup",
                    "help_page_id": "help",
                    "error_help_anchors": {"invalid_client": "credentials"},
                    "authorization_endpoint": {"url": "https://identity.example.test/authorize"},
                    "token_endpoint": {
                        "url": "https://identity.example.test/token",
                        "client_auth_method": "client_secret_post",
                    },
                    "revocation_endpoint": {"url": "https://identity.example.test/revoke"},
                    "credential_fields": [
                        {
                            "field_id": "client_id",
                            "value_type": "text",
                            "required": True,
                            "secret": False,
                            "label": "Application ID",
                            "help_text": "Public identifier.",
                            "placeholder": "client-id",
                            "autocomplete": "username",
                            "validation": {"min_length": 2, "max_length": 200},
                        }
                    ],
                    "authorization_intents": [
                        {
                            "intent_id": "connect",
                            "user_inputs": [
                                {
                                    "input_id": "email_hint",
                                    "validation": {"type": "email", "max_length": 320},
                                }
                            ],
                        }
                    ],
                }
            ]
            assert projected["connection_types"] == manifest["connection_types"]
            assert projected["schemas"] == manifest["schemas"]
            assert projected["action_definitions"][0]["parameters"] == [
                {"name": "display_name", "type": "string"}
            ]
            assert "handler" not in projected["action_definitions"][0]
            assert projected["operation_manifest"] == [
                {
                    "operation_id": "save",
                    "inputs": [{"name": "display_name", "type": "string"}],
                }
            ]
            assert (
                "handler_ref"
                not in projected["extension_contract_v2_normalized"]["operation_manifest"][0]
            )
            assert projected["runtime"] == {
                "extension_contract_v2": {"llm_provider": {"provider_key": "example"}}
            }
            assert not {
                "bundle_root",
                "compile_errors",
                "compile_warnings",
                "entrypoint",
                "healthcheck",
                "manifest_path",
            }.intersection(projected)
            assert "/private/owner" not in json.dumps(projected)

        admin_response = client.get(
            "/api/extensions/acme.viewer-manifest/compiled-manifest",
            headers={"x-test-role": "admin"},
        )
        assert admin_response.status_code == 200
        assert admin_response.json() == manifest

    def test_get_extension_not_found_raises(self):
        _, query_svc, _, _, _ = _make_services()
        with pytest.raises(ServiceNotFoundError):
            query_svc.get_extension("nonexistent", scope=_ADMIN_SCOPE)

    def test_data_only_extension_detail_and_manifest_share_canonical_api_projection(self):
        client, store, runtime_store, _bl = _make_complete_extension_api_client()
        release = runtime_store.create_release(
            {
                "release_id": "pr_data_only",
                "extension_id": "acme.data-only",
                "release_version": "1.0.0",
                "descriptor": {
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "acme.data-only",
                    "release_version": "1.0.0",
                    "extension_type": "data_extension",
                    "identity": {
                        "display_name": "Data Only",
                        "description": "Published datasets without a runtime module",
                    },
                    "artifacts": {"datasets": []},
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_mode="data_only",
        )

        detail_response = client.get("/api/extensions/acme.data-only")
        manifest_response = client.get("/api/extensions/acme.data-only/compiled-manifest")

        assert detail_response.status_code == 200
        assert manifest_response.status_code == 200
        detail = detail_response.json()
        manifest = manifest_response.json()
        assert detail["has_runtime_module"] is False
        assert detail["compiled_manifest"] == manifest
        assert manifest["extension_id"] == "acme.data-only"
        assert manifest["display_name"] == "Data Only"
        assert manifest["ui_manifest"] == {"version": "1", "pages": []}

        query_service = ExtensionQueryService(
            get_logger=lambda: _bl,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            list_extension_projects=lambda *, account_id: _bl.get_projects(account_id=account_id),
        )
        _bl.get_projects.return_value = [
            {"id": "project-1", "name": "Data", "account_id": DEFAULT_ACCOUNT_ID},
            {"id": "project-2", "name": "Foreign", "account_id": "acc_foreign"},
            {"id": "project-3", "name": "Missing account"},
        ]
        _bl.get_projects.reset_mock()
        page = query_service.get_extension_page_context(
            extension_id="acme.data-only",
            selected_project_id="project-1",
            scope=_ADMIN_SCOPE,
        )
        preview = query_service.get_preview("acme.data-only", scope=_ADMIN_SCOPE)
        assert page.compiled_manifest == manifest
        assert page.validation_summary["status"] == "ok"
        assert page.selected_project_id == "project-1"
        assert [project.id for project in page.projects] == ["project-1"]
        assert preview["compiled_manifest"] == manifest
        assert _bl.get_projects.call_count >= 1
        assert all(
            call.kwargs == {"account_id": DEFAULT_ACCOUNT_ID}
            for call in _bl.get_projects.call_args_list
        )

    def test_runtime_health_is_passive_and_uses_persisted_state(self):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "rel-runtime-health",
                "extension_id": "acme.runtime",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.runtime",
                    "runtime": {"contract_version": "extension_host_v2"},
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            health_status="failed",
        )
        resolver = MagicMock(
            return_value=(
                {"extension_key": "acme.runtime", "bundle_root": "/tmp/acme"},
                None,
            )
        )
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=resolver,
            build_extension_compiled_manifest=lambda extension: dict(extension or {}),
            extension_governance_status=lambda _extension_id, **_identity: {"quarantined": False},
        )

        result = service.get_health("acme.runtime", scope=_ADMIN_SCOPE)

        assert result.health["status"] == "failed"
        assert result.health["governance"] == {"quarantined": False}
        resolver.assert_not_called()

    def test_runtime_health_reads_exact_account_install_governance_bucket(self):
        from core.infrastructure.extension_runtime.extension_runtime import (
            ExtensionCircuitBreaker,
        )

        ExtensionCircuitBreaker.reset_all()
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "rel-runtime-governance",
                "extension_id": "acme.runtime-governance",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.runtime-governance",
                    "runtime": {"contract_version": "extension_host_v2"},
                },
            }
        )
        owner = store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=_ADMIN_SCOPE.account_id,
            release_id=release.release_id,
        )
        other = store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=_OTHER_ADMIN_SCOPE.account_id,
            release_id=release.release_id,
        )
        owner_breaker = ExtensionCircuitBreaker.for_extension(
            release.extension_id,
            account_id=_ADMIN_SCOPE.account_id,
            extension_install_id=owner.install_id,
        )
        for _ in range(owner_breaker.failure_threshold):
            assert owner_breaker.pre_invoke() is None
            owner_breaker.post_invoke(succeeded=False)

        def governance_reader(
            extension_id: str,
            *,
            account_id: str,
            extension_install_id: str,
        ) -> dict:
            return ExtensionCircuitBreaker.for_extension(
                extension_id,
                account_id=account_id,
                extension_install_id=extension_install_id,
            ).status()

        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            extension_governance_status=governance_reader,
        )

        owner_health = service.get_health(release.extension_id, scope=_ADMIN_SCOPE)
        other_health = service.get_health(release.extension_id, scope=_OTHER_ADMIN_SCOPE)

        assert owner_health.health["governance"]["quarantined"] is True
        assert owner_health.health["governance"]["extension_install_id"] == owner.install_id
        assert other_health.health["governance"]["quarantined"] is False
        assert other_health.health["governance"]["extension_install_id"] == other.install_id

    def test_runtime_backed_install_uses_runtime_manifest_and_preview_adapters(self):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "pr_runtime_ui",
                "extension_id": "acme.runtime-ui",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.runtime-ui",
                    "release_version": "1.0.0",
                    "identity": {"display_name": "Runtime UI"},
                    "runtime": {"contract_version": "extension_host_v2"},
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
        )
        resolver = MagicMock(
            return_value=({"extension_key": "acme.runtime-ui", "runtime": True}, None)
        )
        builder = MagicMock(
            return_value={
                "extension_id": "acme.runtime-ui",
                "ui_manifest": {"version": "1", "pages": [{"id": "overview"}]},
                "compile_status": "ok",
            }
        )
        preview_compiler = MagicMock(
            return_value={"preview": "compiled", "pages": [{"id": "overview"}]}
        )
        service = ExtensionQueryService(
            get_logger=lambda: MagicMock(),
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=resolver,
            build_extension_compiled_manifest=builder,
            compile_extension_preview=preview_compiler,
        )

        detail = service.get_extension("acme.runtime-ui", scope=_ADMIN_SCOPE)
        manifest = service.get_compiled_manifest("acme.runtime-ui", scope=_ADMIN_SCOPE)
        page = service.get_extension_page_context(
            extension_id="acme.runtime-ui", scope=_ADMIN_SCOPE
        )
        preview = service.get_preview("acme.runtime-ui", scope=_ADMIN_SCOPE)

        assert detail.has_runtime_module is True
        assert detail.compiled_manifest["ui_manifest"]["pages"] == [{"id": "overview"}]
        assert manifest == detail.compiled_manifest
        assert page.compiled_manifest == manifest
        assert preview == {"preview": "compiled", "pages": [{"id": "overview"}]}
        assert builder.call_count >= 1
        preview_compiler.assert_called_once()
        assert resolver.call_args_list
        assert all(
            call.kwargs == {"account_id": DEFAULT_ACCOUNT_ID} for call in resolver.call_args_list
        )

    def test_bundle_contract_v2_install_still_resolves_runtime_ui_manifest(self):
        # Regression: bundle-built releases carry runtime.extension_contract_v2 and
        # release.execution_modes instead of runtime.contract_version. They must
        # still count as runtime-backed, otherwise detail/compiled-manifest fall
        # back to the descriptor-only projection and UI pages disappear.
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "pr_bundle_v2",
                "extension_id": "acme.bundle-v2",
                "release_version": "1.0.0",
                "execution_modes": ["subprocess"],
                "descriptor": {
                    "extension_id": "acme.bundle-v2",
                    "release_version": "1.0.0",
                    "identity": {"display_name": "Bundle V2"},
                    "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
                    "runtime": {
                        "extension_contract_v2": {
                            "operation_manifest": "contracts/operation_manifest.yaml"
                        }
                    },
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
        )
        resolver = MagicMock(
            return_value=({"extension_key": "acme.bundle-v2", "runtime": True}, None)
        )
        builder = MagicMock(
            return_value={
                "extension_id": "acme.bundle-v2",
                "ui_manifest": {"version": "1", "pages": [{"id": "settings"}]},
                "compile_status": "ok",
            }
        )
        service = ExtensionQueryService(
            get_logger=lambda: MagicMock(),
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=resolver,
            build_extension_compiled_manifest=builder,
        )

        detail = service.get_extension("acme.bundle-v2", scope=_ADMIN_SCOPE)
        manifest = service.get_compiled_manifest("acme.bundle-v2", scope=_ADMIN_SCOPE)

        assert detail.has_runtime_module is True
        assert manifest["ui_manifest"]["pages"] == [{"id": "settings"}]

    def test_runtime_only_page_context_uses_scoped_runtime_projection(self):
        resolver = MagicMock(
            return_value=(
                {
                    "extension_key": "acme.runtime-only",
                    "display_name": "Runtime Only",
                    "description": "Legacy runtime discovery record",
                    "version": "2.0.0",
                    "kind": "service_extension",
                    "state": "active",
                },
                None,
            )
        )
        service = ExtensionQueryService(
            get_logger=lambda: MagicMock(get_projects=list),
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=resolver,
            build_extension_compiled_manifest=lambda _extension: {
                "extension_id": "acme.runtime-only",
                "ui_manifest": {"version": "1", "pages": [{"id": "runtime"}]},
                "compile_status": "ok",
            },
        )

        page = service.get_extension_page_context(
            extension_id="acme.runtime-only",
            locale="en",
            scope=_ADMIN_SCOPE,
        )

        assert page.extension_id == "acme.runtime-only"
        assert page.extension_name == "Runtime Only"
        assert page.compiled_manifest["ui_manifest"]["pages"] == [{"id": "runtime"}]
        resolver.assert_called_with("acme.runtime-only", account_id=DEFAULT_ACCOUNT_ID)

    def test_runtime_fallbacks_never_cross_account_scope(self):
        requested_accounts: list[str | None] = []

        def resolve(_extension_id: str, *, account_id: str | None = None):
            requested_accounts.append(account_id)
            if account_id == DEFAULT_ACCOUNT_ID:
                return ({"extension_key": "acme.private"}, None)
            return (None, None)

        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=resolve,
            build_extension_compiled_manifest=lambda extension: dict(extension or {}),
            compile_extension_preview=lambda extension: dict(extension),
        )

        client, _, _, _ = _make_complete_extension_api_client(query_service=service)
        foreign_headers = {"x-test-account-id": _OTHER_ADMIN_SCOPE.account_id}
        for path in (
            "/api/extensions/acme.private",
            "/api/extensions/acme.private/compiled-manifest",
            "/api/extensions/acme.private/preview",
            "/api/extensions/acme.private/health",
        ):
            response = client.get(path, headers=foreign_headers)
            assert response.status_code == 404

        with pytest.raises(ServiceNotFoundError):
            service.get_extension_page_context(
                extension_id="acme.private", scope=_OTHER_ADMIN_SCOPE
            )

        assert len(requested_accounts) >= 4
        assert set(requested_accounts) == {_OTHER_ADMIN_SCOPE.account_id}

    def test_runtime_fallback_without_scope_is_limited_to_default_account(self):
        requested_accounts: list[str | None] = []

        def resolve(_extension_id: str, *, account_id: str | None = None):
            requested_accounts.append(account_id)
            if account_id is None:
                return ({"extension_key": "acme.foreign-private"}, None)
            return (None, None)

        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=resolve,
        )

        with pytest.raises(ServiceNotFoundError):
            service.get_extension("acme.foreign-private")

        assert requested_accounts == [DEFAULT_ACCOUNT_ID]

    def test_foreign_account_global_detail_redacts_owner_install_state(self):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "pr_global_public",
                "extension_id": "acme.global-public",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.global-public",
                    "release_version": "1.0.0",
                    "identity": {
                        "display_name": "Global Public",
                        "description": "Safe public description",
                        "owner_secret": "identity-owner-secret",
                    },
                    "vendor": {
                        "name": "Acme",
                        "support_url": "https://support.example",
                        "private_key_path": "/private/owner/vendor.key",
                    },
                    "listing": {
                        "category": "automation",
                        "tags": ["public", {"token": "nested-tag-owner-secret"}],
                        "internal_notes": "listing-owner-secret",
                    },
                    "runtime": {
                        "contract_version": "extension_host_v2",
                        "entrypoint": "/private/owner/runtime.py",
                        "health_command": ["cat", "/private/owner/health-token"],
                    },
                    "bundle_root": "/private/owner/extensions/global-public",
                    "arbitrary_extra": {"token": "descriptor-owner-secret"},
                },
                "account_id": DEFAULT_ACCOUNT_ID,
                "created_by": "owner-user",
                "source_ref": "/private/owner/extensions/global-public",
            }
        )
        owner_install = store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            visibility="global",
            release_id=release.release_id,
            config={"api_token_ref": "secret://owner/token"},
            remote_base_url="https://owner.internal.example",
            health_status="failed",
            last_error="owner-only failure detail",
        )
        runtime_store.upsert_entitlement(
            install_id=owner_install.install_id,
            account_id=DEFAULT_ACCOUNT_ID,
            metadata={"billing_customer_id": "cus_owner_private"},
        )
        runtime_store.replace_artifact_snapshots(
            release.release_id,
            [
                {
                    "release_id": release.release_id,
                    "artifact_kind": "runtime_module",
                    "artifact_id": "runtime-main",
                    "artifact_ref": "/private/owner/runtime.py",
                    "publish": True,
                    "snapshot": {
                        "entrypoint": "/private/owner/runtime.py",
                        "token": "artifact-owner-secret",
                    },
                }
            ],
        )
        preview_compiler = MagicMock(return_value={"preview": "/private/owner/preview.html"})
        governance_reader = MagicMock(
            return_value={"quarantined": True, "owner_state": "runtime-owner-secret"}
        )
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                {
                    "extension_key": extension_id,
                    "bundle_root": "/private/owner/extensions/global-public",
                    "manifest_path": "/private/owner/extensions/global-public/extension.yaml",
                    "compiled_manifest": {
                        "extension_id": extension_id,
                        "owner_secret": "manifest-owner-secret",
                        "ui_manifest": {"version": "1", "pages": []},
                    },
                    "owner_metadata": "runtime-owner-secret",
                },
                None,
            ),
            build_extension_compiled_manifest=lambda runtime: dict(runtime["compiled_manifest"]),
            compile_extension_preview=preview_compiler,
            extension_governance_status=governance_reader,
        )
        client, _, _, _ = _make_complete_extension_api_client(
            store=store,
            runtime_store=runtime_store,
            query_service=service,
        )

        response = client.get(
            "/api/extensions/acme.global-public",
            headers={"x-test-account-id": _OTHER_ADMIN_SCOPE.account_id},
        )

        assert response.status_code == 200
        detail = response.json()
        assert detail["extension_id"] == "acme.global-public"
        assert detail["display_name"] == "Global Public"
        assert detail["visibility"] == "global"
        assert detail["config"] == {}
        assert detail["entitlement"] is None
        assert detail["connections"] == []
        assert detail["projections"] == []
        assert detail["install_id"] == ""
        assert detail["remote_base_url"] == ""
        assert detail["health_status"] == ""
        assert detail["runtime_status"] == ""
        assert detail["handshake_status"] == ""
        public_release = detail["metadata"]["release"]
        assert "account_id" not in public_release
        assert "created_by" not in public_release
        assert "source_ref" not in public_release
        serialized = json.dumps(detail, sort_keys=True)
        assert "secret://owner/token" not in serialized
        assert "cus_owner_private" not in serialized
        assert "owner-only failure detail" not in serialized
        assert "descriptor-owner-secret" not in serialized
        assert "artifact-owner-secret" not in serialized
        assert "identity-owner-secret" not in serialized
        assert "listing-owner-secret" not in serialized
        assert "nested-tag-owner-secret" not in serialized
        assert "artifact_ref" not in serialized
        assert "snapshot" not in serialized
        assert "entrypoint" not in serialized
        assert "health_command" not in serialized
        assert "/private/owner" not in serialized
        for suffix in ("compiled-manifest", "preview", "health"):
            projected = client.get(
                f"/api/extensions/acme.global-public/{suffix}",
                headers={"x-test-account-id": _OTHER_ADMIN_SCOPE.account_id},
            )
            assert projected.status_code == 200
            projected_json = json.dumps(projected.json(), sort_keys=True)
            assert "/private/owner" not in projected_json
            assert "manifest-owner-secret" not in projected_json
            assert "runtime-owner-secret" not in projected_json
            assert "descriptor-owner-secret" not in projected_json
            assert "artifact-owner-secret" not in projected_json
            assert "artifact_ref" not in projected_json
            assert "snapshot" not in projected_json
            assert "entrypoint" not in projected_json
            assert "health_command" not in projected_json
        preview_compiler.assert_not_called()
        governance_reader.assert_not_called()
        list_response = client.get(
            "/api/extensions",
            headers={"x-test-account-id": _OTHER_ADMIN_SCOPE.account_id},
        )
        assert list_response.status_code == 200
        summary = list_response.json()["extensions"][0]
        assert summary["install_id"] == ""
        assert summary["remote_base_url"] == ""
        assert summary["health_status"] == ""
        assert "secret://owner/token" not in json.dumps(summary, sort_keys=True)

    def test_api_detail_and_manifest_use_same_fully_configured_query_provider(self):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "pr_api_runtime",
                "extension_id": "acme.api-runtime",
                "release_version": "1.0.0",
                "descriptor": {
                    "extension_id": "acme.api-runtime",
                    "release_version": "1.0.0",
                    "runtime": {"contract_version": "extension_host_v2"},
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
        )
        manifest = {
            "extension_id": "acme.api-runtime",
            "ui_manifest": {"version": "1", "pages": [{"id": "runtime"}]},
            "compile_status": "ok",
        }
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                {"extension_key": extension_id},
                None,
            ),
            build_extension_compiled_manifest=lambda _runtime: manifest,
        )
        client, _, _, _ = _make_complete_extension_api_client(
            store=store,
            runtime_store=runtime_store,
            query_service=service,
        )

        detail_response = client.get("/api/extensions/acme.api-runtime")
        manifest_response = client.get("/api/extensions/acme.api-runtime/compiled-manifest")

        assert detail_response.status_code == 200
        assert manifest_response.status_code == 200
        assert detail_response.json()["compiled_manifest"] == manifest_response.json()
        assert detail_response.json()["compiled_manifest"]["ui_manifest"]["pages"] == [
            {"id": "runtime"}
        ]

    def test_entitlement_lookup_never_retries_without_account_scope(self):
        store = _make_install_store()
        install = store.install(
            extension_id="acme.scoped-entitlement",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )

        class BrokenRuntimeStore:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def list_entitlements(self, **kwargs):
                self.calls.append(dict(kwargs))
                if "account_id" in kwargs:
                    raise TypeError("scoped signature is required")
                return [
                    MagicMock(
                        install_id=install.install_id,
                        account_id=_OTHER_ADMIN_SCOPE.account_id,
                    )
                ]

            def list_releases(self, **_kwargs):
                return []

            def list_artifact_snapshots_for_releases(self, _release_ids):
                return []

        runtime_store = BrokenRuntimeStore()
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        )

        with pytest.raises(ServiceUnavailableError, match="entitlement store"):
            service.list_extensions(scope=_ADMIN_SCOPE)

        assert runtime_store.calls == [
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "install_ids": [install.install_id],
            }
        ]

    def test_projection_corruption_is_not_masked_as_runtime_fallback(self):
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                {"extension_key": extension_id},
                None,
            ),
            build_extension_compiled_manifest=lambda runtime: dict(runtime or {}),
        )
        service._details.get_extension = MagicMock(
            side_effect=ValueError("corrupt canonical projection")
        )

        for query in (
            lambda: service.get_compiled_manifest("acme.corrupt", scope=_ADMIN_SCOPE),
            lambda: service.get_preview("acme.corrupt", scope=_ADMIN_SCOPE),
            lambda: service.get_health("acme.corrupt", scope=_ADMIN_SCOPE),
        ):
            with pytest.raises(ValueError, match="corrupt canonical projection"):
                query()

    def test_runtime_only_extension_is_visible_in_canonical_list_and_detail(self):
        runtime = {
            "extension_key": "acme.copied-runtime",
            "display_name": "Copied Runtime",
            "version": "3.0.0",
            "kind": "service_extension",
            "state": "disabled",
        }
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                (runtime, None) if extension_id == "acme.copied-runtime" else (None, None)
            ),
            list_extension_runtime_records=lambda *, account_id: [runtime],
            build_extension_compiled_manifest=lambda _runtime: {
                "extension_id": "acme.copied-runtime",
                "ui_manifest": {"version": "1", "pages": [{"id": "copied"}]},
                "compile_status": "ok",
            },
        )

        listing = service.list_extensions(scope=_ADMIN_SCOPE)
        detail = service.get_extension("acme.copied-runtime", scope=_ADMIN_SCOPE)

        assert listing.total == 1
        assert listing.extensions[0].extension_id == "acme.copied-runtime"
        assert listing.extensions[0].state == "disabled"
        assert detail.extension_id == "acme.copied-runtime"
        assert detail.compiled_manifest["ui_manifest"]["pages"] == [{"id": "copied"}]
        client, _, _, _ = _make_complete_extension_api_client(query_service=service)
        list_response = client.get("/api/extensions")
        detail_response = client.get("/api/extensions/acme.copied-runtime")
        assert list_response.status_code == 200
        assert list_response.json()["extensions"][0]["extension_id"] == ("acme.copied-runtime")
        assert detail_response.status_code == 200
        assert detail_response.json()["compiled_manifest"]["ui_manifest"]["pages"] == [
            {"id": "copied"}
        ]

    def test_data_only_health_reports_no_runtime_component(self):
        store = _make_install_store()
        store.install(
            extension_id="acme.data-health",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            runtime_mode="data_only",
        )
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=_make_runtime_store,
        )

        result = service.get_health("acme.data-health", scope=_ADMIN_SCOPE)

        assert result.health == {
            "status": "not_applicable",
            "detail": "Extension has no runtime component.",
        }

    def test_runtime_health_without_persisted_observation_is_unknown(self):
        runtime = {
            "extension_key": "acme.unobserved-runtime",
            "state": "active",
        }
        service = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=_make_install_store,
            get_runtime_store=_make_runtime_store,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                runtime if extension_id == "acme.unobserved-runtime" else None,
                None,
            ),
        )

        result = service.get_health("acme.unobserved-runtime", scope=_ADMIN_SCOPE)

        assert result.health["status"] == "unknown"
        assert "no active probe" in result.health["detail"]

    def test_runtime_health_query_runs_canonical_probe_and_normalizes_success(self):
        runtime = {
            "extension_key": "acme.probed-runtime",
            "display_name": "Probed Runtime",
            "enabled": True,
            "bundle_root": "/tmp/acme-probed-runtime",
            "healthcheck": {"command": ["python3", "health.py"]},
        }
        runner = MagicMock(
            return_value={
                "status": "healthy",
                "detail": "All runtime checks passed.",
                "exit_code": 0,
            }
        )
        service = ExtensionQueryService(
            get_logger=lambda: None,
            resolve_extension_runtime_record=lambda extension_id, *, account_id=None: (
                runtime if extension_id == "acme.probed-runtime" else None,
                None,
            ),
            run_extension_health=runner,
        )

        result = service.get_health("acme.probed-runtime", scope=_ADMIN_SCOPE)

        assert result.health["status"] == "healthy"
        assert result.health["reason"] == "All runtime checks passed."
        assert result.health["checked_at"]
        runner.assert_called_once_with(runtime)


class TestExtensionRuntimeFoundation:
    @staticmethod
    def _write_descriptor(
        tmp_path,
        *,
        execution_modes: list[str],
        with_remote: bool = False,
        extension_id: str = "acme.runtime-extension",
        release_version: str = "1.0.0",
        dependencies: dict[str, Any] | None = None,
    ):
        from core.application.extensions.service_support import _runtime_platform_version

        (tmp_path / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
        (tmp_path / "artifacts" / "agents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "artifacts" / "workflows" / "wf_sync.yaml").write_text(
            "workflow_id: wf_sync\n", encoding="utf-8"
        )
        (tmp_path / "artifacts" / "agents" / "agent_sync.yaml").write_text(
            "agent_id: agent_sync\n", encoding="utf-8"
        )
        descriptor = {
            "schema_version": "extension_descriptor_v1",
            "extension_id": extension_id,
            "release_version": release_version,
            "extension_type": "service_extension",
            "identity": {
                "display_name": "Runtime Extension",
                "description": "desc",
                "tags": ["runtime"],
            },
            "vendor": {"vendor_name": "Acme", "support_url": "https://acme.example.com/support"},
            "distribution": {
                "channel": "private_byo",
                "execution_modes": execution_modes,
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file", "url"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": _runtime_platform_version()},
            },
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": "wf_sync",
                        "ref": "artifacts/workflows/wf_sync.yaml",
                        "publish": True,
                    }
                ],
                "agents": [
                    {
                        "agent_id": "agent_sync",
                        "ref": "artifacts/agents/agent_sync.yaml",
                        "publish": True,
                    }
                ],
            },
            "provenance": {"origin": "imported"},
        }
        if dependencies is not None:
            descriptor["dependencies"] = dependencies
        if with_remote:
            descriptor["runtime"]["remote"] = {
                "base_url": "https://runtime.example.com/base",
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/queries/{query_id}",
                    "action": "/actions/{action_id}",
                    "emit_event": "/events",
                },
            }
        descriptor_path = tmp_path / "extension-descriptor.json"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        return descriptor_path

    @pytest.mark.parametrize(
        "extension_id",
        [
            "legacy_notifier",
            "legacy_example",
            "Example Service",
            "example service",
            "example_service",
        ],
    )
    def test_descriptor_rejects_non_canonical_extension_id(self, tmp_path, extension_id):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path,
            execution_modes=["local"],
            extension_id=extension_id,
        )

        _raises_input_2370_1 = ExtensionDescriptorImportFileInput(
            descriptor_path=str(descriptor_path)
        )
        with pytest.raises(ServiceValidationError):
            cmd_svc.import_descriptor_file(_raises_input_2370_1, scope=_ADMIN_SCOPE)

    @pytest.mark.parametrize(
        "extension_id",
        ["acme.notification-provider", "acme.example-provider"],
    )
    def test_descriptor_accepts_canonical_extension_id(self, tmp_path, extension_id):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path,
            execution_modes=["local"],
            extension_id=extension_id,
        )

        result = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )

        assert result.extension_id == extension_id
        assert runtime_store.get_release(result.release_id) is not None

    def test_import_descriptor_file_persists_release_and_snapshots(self, tmp_path):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])

        result = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )

        assert result.status == "imported"
        assert result.release_id
        saved_release = runtime_store.get_release(result.release_id)
        assert saved_release is not None
        snapshots = runtime_store.list_artifact_snapshots(result.release_id)
        assert len(snapshots) == 2

    def test_dataset_and_saved_query_refs_materialize_into_install_owned_projections(
        self, tmp_path
    ):
        registry_store = MagicMock()
        registry_store.get_dataset.return_value = None
        registry_store.get_dataset_query_template.return_value = None
        bl = MagicMock()
        bl.db_store = registry_store
        cmd_svc, query_svc, _store, runtime_store, _ = _make_services(bl=bl)
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        (tmp_path / "artifacts" / "datasets").mkdir(parents=True)
        (tmp_path / "artifacts" / "saved_queries").mkdir(parents=True)
        dataset = {
            "dataset_id": "orders_dataset",
            "display_name": "Orders",
            "connector_id": "extension.connection",
            "resource_scope_jsonb": {
                "resources": [{"resource_id": "orders", "columns": ["id", "closed"]}]
            },
        }
        saved_query = {
            "query_template_id": "open_orders",
            "dataset_id": "orders_dataset",
            "display_name": "Open orders",
            "query_definition_jsonb": {"sql": "SELECT id FROM orders WHERE closed = false"},
        }
        (tmp_path / "artifacts" / "datasets" / "orders.yaml").write_text(
            "dataset_id: orders_dataset\n"
            "display_name: Orders\n"
            "connector_id: extension.connection\n"
            "resource_scope_jsonb:\n"
            "  resources:\n"
            "    - resource_id: orders\n"
            "      columns: [id, closed]\n",
            encoding="utf-8",
        )
        (tmp_path / "artifacts" / "saved_queries" / "open_orders.json").write_text(
            json.dumps(saved_query), encoding="utf-8"
        )
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["artifacts"].update(
            {
                "datasets": [
                    {
                        "dataset_id": "orders_dataset",
                        "ref": "artifacts/datasets/orders.yaml",
                        "publish": True,
                    }
                ],
                "saved_queries": [
                    {
                        "query_template_id": "open_orders",
                        "ref": "artifacts/saved_queries/open_orders.json",
                        "publish": True,
                    }
                ],
            }
        )
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            ),
            scope=_ADMIN_SCOPE,
        )

        snapshots = {
            (row.artifact_kind, row.artifact_id): row.snapshot
            for row in runtime_store.list_artifact_snapshots(imported.release_id)
        }
        assert snapshots[("dataset", "orders_dataset")] == dataset
        assert snapshots[("saved_query", "open_orders")] == saved_query
        detail = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        projections = {
            (row["artifact_kind"], row["artifact_id"]): row for row in detail.projections
        }
        install_id = detail.install_id
        assert projections[("dataset", "orders_dataset")]["install_id"] == install_id
        assert projections[("dataset", "orders_dataset")]["projection"] == dataset
        assert projections[("saved_query", "open_orders")]["install_id"] == install_id
        assert projections[("saved_query", "open_orders")]["projection"] == saved_query
        dataset_bridge = registry_store.save_dataset.call_args.args[0]
        query_bridge = registry_store.save_dataset_query_template.call_args.args[0]
        assert dataset_bridge["dataset_id"] == f"extension.{install_id}.orders_dataset"
        assert dataset_bridge["metadata_jsonb"]["projection_bridge"]["install_id"] == install_id
        assert query_bridge["query_template_id"] == f"extension.{install_id}.open_orders"
        assert query_bridge["dataset_id"] == f"extension.{install_id}.orders_dataset"

    def test_enable_repairs_all_missing_projections_without_duplicates(self, tmp_path):
        """Built here, not read from a shipped bundle.

        This used to import the showcase bundle and assert its exact artifact
        counts, which pinned that bundle's contents inside the platform suite.
        What the platform owns is that enable repairs every declared projection
        and does not duplicate it, whatever the counts are.
        """
        from tests.support.synthetic_extension_bundle import descriptor_path as _descriptor_path
        from tests.support.synthetic_extension_bundle import projection_counts

        cmd_svc, query_svc, store, runtime_store, _ = _make_services(
            bl=SimpleNamespace(conn=None, db_store=None, dsn="")
        )
        expected_counts = projection_counts()
        descriptor_path = _descriptor_path(tmp_path)
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            ),
            scope=_ADMIN_SCOPE,
        )
        install = store.get(
            extension_id=imported.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert install is not None
        cmd_svc.disable_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        runtime_store.delete_projections_for_install(install.install_id)
        store.set_state(
            extension_id=imported.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
            state="active",
        )
        degraded = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        assert degraded.metadata["projection_health"]["status"] == "degraded"
        assert len(degraded.metadata["projection_health"]["missing"]) == sum(
            expected_counts.values()
        )
        store.set_state(
            extension_id=imported.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
            state="disabled",
        )

        first = cmd_svc.enable_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        second = cmd_svc.enable_extension(imported.extension_id, scope=_ADMIN_SCOPE)

        assert first.state == second.state == "active"
        detail = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        counts = {
            kind: sum(row["artifact_kind"] == kind for row in detail.projections)
            for kind in ("workflow", "agent", "skill", "tool", "dataset", "saved_query")
        }
        assert counts == expected_counts
        assert detail.metadata["projection_health"] == {
            "status": "ok",
            "declared": counts,
            "active": counts,
            "missing": [],
            "unexpected": [],
        }

    def test_file_import_never_trusts_a_descriptor_provenance_origin(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Whatever origin a descriptor claims, a file import records what it is."""
        from tests.support.synthetic_extension_bundle import write_bundle

        extensions_root = tmp_path / "extensions"
        extensions_root.mkdir()
        outside_bundle = write_bundle(tmp_path / "outside")
        descriptor_path = outside_bundle / "extension-descriptor.yaml"
        descriptor_path.write_text(
            descriptor_path.read_text(encoding="utf-8").replace(
                "  origin: imported", "  origin: vendor_published", 1
            ),
            encoding="utf-8",
        )
        assert "  origin: vendor_published" in descriptor_path.read_text(encoding="utf-8")
        monkeypatch.setenv("FS_EXTENSIONS_ROOT", str(extensions_root))
        cmd_svc, _, _, runtime_store, _ = _make_services(
            bl=SimpleNamespace(conn=None, db_store=None, dsn="")
        )

        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=False
            ),
            scope=_ADMIN_SCOPE,
        )

        release = runtime_store.get_release(imported.release_id)
        assert release.provenance["origin"] == "imported"

    def test_enable_enriches_a_clean_scanned_install_without_explicit_import(self, tmp_path):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.infrastructure.extension_runtime.extension_runtime_compile import (
            compile_extension_bundle,
        )
        from tests.support.synthetic_extension_bundle import projection_counts, write_bundle

        provider_rows: dict[str, dict] = {}
        cmd_svc, query_svc, store, runtime_store, _ = _make_services(
            bl=SimpleNamespace(
                conn=None,
                db_store=None,
                dsn="",
                get_provider=provider_rows.get,
                save_provider=lambda row: provider_rows.update(
                    {row["provider_key"]: copy.deepcopy(row)}
                ),
            )
        )
        expected_counts = projection_counts()
        bundle_root = write_bundle(tmp_path)
        scanned = compile_extension_bundle(bundle_root)
        assert scanned["status"] == "valid"
        release = runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id="clean-showcase-scan-release",
                extension_id="acme.showcase",
                release_version="1.0.0",
                descriptor=scanned["compiled_manifest"],
                source_kind="extension_scan",
                source_ref="acme.showcase",
                account_id=DEFAULT_ACCOUNT_ID,
            )
        )
        install = store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            descriptor_source="extension_scan",
        )
        store.set_state(
            extension_id=release.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
            state="disabled",
        )
        assert runtime_store.list_artifact_snapshots(release.release_id) == []
        assert runtime_store.list_projections(account_id=DEFAULT_ACCOUNT_ID) == []

        first = cmd_svc.enable_extension(release.extension_id, scope=_ADMIN_SCOPE)
        second = cmd_svc.enable_extension(release.extension_id, scope=_ADMIN_SCOPE)

        assert first.state == second.state == "active"
        assert provider_rows["acme.showcase"]["provider_type_key"] == "tool_provider"
        assert provider_rows["acme.showcase"]["enabled"] is True
        detail = query_svc.get_install_detail(install.install_id, scope=_ADMIN_SCOPE)
        counts = {
            kind: sum(row["artifact_kind"] == kind for row in detail.projections)
            for kind in ("workflow", "agent", "skill", "tool", "dataset", "saved_query")
        }
        assert counts == expected_counts
        assert len(detail.projections) == sum(expected_counts.values())

    def test_enable_projection_failure_preserves_disabled_state_and_empty_projection_set(
        self, tmp_path
    ):
        cmd_svc, _, store, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            ),
            scope=_ADMIN_SCOPE,
        )
        install = store.get(
            extension_id=imported.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert install is not None
        cmd_svc.disable_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        runtime_store.delete_projections_for_install(install.install_id)
        original_upsert = runtime_store.upsert_projection
        writes = 0

        def fail_second_projection(payload):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError("injected enable projection failure")
            return original_upsert(payload)

        runtime_store.upsert_projection = fail_second_projection
        try:
            with pytest.raises(RuntimeError, match="injected enable projection failure"):
                cmd_svc.enable_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        finally:
            runtime_store.upsert_projection = original_upsert

        persisted = store.get(
            extension_id=imported.extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert persisted is not None
        assert persisted.state.value == "disabled"
        assert (
            runtime_store.list_projections(
                account_id=DEFAULT_ACCOUNT_ID,
                only_active=False,
            )
            == []
        )

    def test_published_workflows_are_available_from_the_registry_after_install(self, tmp_path):
        """AUDIT-048: published workflow artifacts must be launchable after install."""
        from core.application.registry.builtin_standard_tools import (
            builtin_standard_tool_manifests,
        )
        from core.application.registry.contract_validation import validate_workflow_readiness
        from core.application.registry.skill_registry import SkillRegistry
        from core.application.registry.workflow_registry import WorkflowRegistry
        from core.entrypoints.admin.workflows_projection import _WorkflowValidationDBView
        from tests.support.synthetic_extension_bundle import (
            descriptor_path as _synthetic_descriptor_path,
        )
        from tests.support.synthetic_extension_bundle import projection_counts

        runtime_store = _make_runtime_store()
        standard_tools = builtin_standard_tool_manifests()
        registry_store = SimpleNamespace(
            extension_runtime_store=runtime_store,
            get_agent=lambda _agent_id: None,
            get_agent_inputs=lambda _agent_id: [],
            get_agent_outputs=lambda _agent_id: [],
            get_agent_skills=lambda _agent_id: [],
            get_published_block_contract=lambda _block_id: None,
            get_skill=lambda _skill_id: None,
            get_skill_inputs=lambda _skill_id: [],
            get_skill_outputs=lambda _skill_id: [],
            get_tool=lambda tool_id: standard_tools.get(tool_id),
            get_workflow=lambda _workflow_id: None,
            list_skills=list,
            list_workflows=list,
            list_project_workflows=lambda **_kwargs: [],
        )
        bl = MagicMock()
        bl.db_store = registry_store
        cmd_svc, query_svc, _store, _runtime_store, _ = _make_services(
            runtime_store=runtime_store, bl=bl
        )
        descriptor_path = _synthetic_descriptor_path(tmp_path)

        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            ),
            scope=_ADMIN_SCOPE,
        )

        detail = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        install_id = detail.install_id
        workflow_projections = [
            row for row in detail.projections if row["artifact_kind"] == "workflow"
        ]
        agent_projections = {
            row["artifact_id"]: row["projection"]
            for row in detail.projections
            if row["artifact_kind"] == "agent"
        }
        registry = WorkflowRegistry(registry_store)
        skill_registry = SkillRegistry(registry_store)
        workflow_ids = {workflow.workflow_id for workflow in registry.list_enabled()}

        assert len(workflow_projections) == projection_counts()["workflow"]
        assert {
            f"extension.{install_id}.{row['artifact_id']}" for row in workflow_projections
        } <= workflow_ids
        assert all(
            registry.get_workflow(f"extension.{install_id}.{row['artifact_id']}").steps
            for row in workflow_projections
        )
        for agent_id, projection in agent_projections.items():
            assert projection["tools"] == ["acme_tool_1"], agent_id
        projected_skills = [
            skill
            for skill in skill_registry.list_enabled()
            if skill.skill_id.startswith(f"extension.{install_id}.")
        ]
        assert projected_skills
        assert all(skill.input_schema and skill.output_schema for skill in projected_skills)
        readiness_errors = {
            workflow_id: validate_workflow_readiness(registry_store, workflow_id)["errors"]
            for workflow_id in workflow_ids
            if workflow_id.startswith(f"extension.{install_id}.acme_")
        }
        assert readiness_errors
        for workflow_id, errors in readiness_errors.items():
            assert errors == [], (workflow_id, errors)
        for row in workflow_projections:
            workflow_id = f"extension.{install_id}.{row['artifact_id']}"
            projected_workflow = dict(row["projection"])
            projected_workflow["workflow_id"] = workflow_id
            view = _WorkflowValidationDBView(
                registry_store,
                workflow_id,
                projected_workflow,
            )
            assert validate_workflow_readiness(view, workflow_id)["errors"] == []

    def test_artifact_dependency_readiness_recovers_when_provider_is_enabled(self, tmp_path):
        cmd_svc, query_svc, _store, _runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        (tmp_path / "artifacts" / "workflows" / "wf_sync.yaml").write_text(
            "\n".join(
                [
                    "workflow_id: wf_sync",
                    "dependencies:",
                    "  extensions:",
                    "    - extension_id: acme.feature-provider",
                    "      required: true",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            ),
            scope=_ADMIN_SCOPE,
        )

        before = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        workflow_before = next(
            row for row in before.projections if row["artifact_kind"] == "workflow"
        )
        assert workflow_before["readiness"] == {
            "available": False,
            "reason": ("Required extension is not installed or enabled: acme.feature-provider"),
            "missing_extensions": ["acme.feature-provider"],
        }
        agent_before = next(row for row in before.projections if row["artifact_kind"] == "agent")
        assert agent_before["readiness"]["available"] is True

        _store.install(
            extension_id="acme.feature-provider",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )

        after = query_svc.get_extension(imported.extension_id, scope=_ADMIN_SCOPE)
        workflow_after = next(
            row for row in after.projections if row["artifact_kind"] == "workflow"
        )
        assert workflow_after["readiness"] == {
            "available": True,
            "reason": "",
            "missing_extensions": [],
        }

    def test_import_descriptor_file_changed_content_creates_new_immutable_release(self, tmp_path):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])

        first = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        descriptor_payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor_payload["identity"]["description"] = "descriptor v2"
        descriptor_path.write_text(json.dumps(descriptor_payload), encoding="utf-8")

        second = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        assert first.release_id != second.release_id
        first_release = runtime_store.get_release(first.release_id)
        second_release = runtime_store.get_release(second.release_id)
        assert first_release is not None
        assert second_release is not None
        assert first_release.descriptor["identity"]["description"] == "desc"
        assert second_release.descriptor["identity"]["description"] == "descriptor v2"

    def test_import_descriptor_blocks_same_extension_id_from_different_source(self, tmp_path):
        cmd_svc, _, _, _runtime_store, _ = _make_services()
        first_path = self._write_descriptor(
            tmp_path / "first",
            execution_modes=["local"],
            extension_id="acme.collision",
        )
        second_path = self._write_descriptor(
            tmp_path / "second",
            execution_modes=["local"],
            extension_id="acme.collision",
            release_version="1.1.0",
        )
        cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(first_path)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_2885_1 = ExtensionDescriptorImportFileInput(descriptor_path=str(second_path))
        with pytest.raises(ServiceValidationError, match="Extension id collision"):
            cmd_svc.import_descriptor_file(_raises_input_2885_1, scope=_ADMIN_SCOPE)

    def test_import_descriptor_file_uses_runtime_platform_version(self, tmp_path, monkeypatch):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        descriptor_payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor_payload["runtime"]["compatibility"]["platform_min"] = "1.1.0"
        descriptor_path.write_text(json.dumps(descriptor_payload), encoding="utf-8")

        monkeypatch.setenv("FS_VERSION", "1.1.0")
        result = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        assert result.status == "imported"
        assert runtime_store.get_release(result.release_id) is not None

    def test_import_descriptor_url_uses_runtime_platform_version(self, monkeypatch):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_data = {
            "schema_version": "extension_descriptor_v1",
            "extension_id": "acme.runtime-extension",
            "release_version": "1.0.0",
            "extension_type": "service_extension",
            "identity": {
                "display_name": "Runtime Extension",
                "description": "desc",
                "tags": ["runtime"],
            },
            "vendor": {"vendor_name": "Acme", "support_url": "https://acme.example.com/support"},
            "distribution": {
                "channel": "private_byo",
                "execution_modes": ["local"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "import_sources": ["file", "url"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": "1.1.0"},
            },
            "artifacts": {
                "workflows": [
                    {
                        "workflow_id": "wf_sync",
                        "snapshot": {"workflow_id": "wf_sync"},
                        "publish": True,
                    }
                ],
                "agents": [
                    {
                        "agent_id": "agent_sync",
                        "snapshot": {"agent_id": "agent_sync"},
                        "publish": True,
                    }
                ],
            },
            "provenance": {"origin": "imported"},
        }

        monkeypatch.setenv("FS_VERSION", "1.1.0")
        monkeypatch.setattr(
            "core.application.extensions.lifecycle._fetch_url_text",
            lambda _url, *, timeout_seconds=20.0: json.dumps(descriptor_data),
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle._probe_url_exists",
            lambda _url, *, timeout_seconds=10.0: True,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )

        result = cmd_svc.import_descriptor_url(
            ExtensionDescriptorImportUrlInput(descriptor_url="https://example.com/descriptor.json"),
            scope=_ADMIN_SCOPE,
        )
        assert result.status == "imported"

    def test_import_descriptor_url_validation_failure_raises(self, monkeypatch):
        cmd_svc, _, _, _, _ = _make_services()

        def _fetch(_url: str, *, timeout_seconds: float = 20.0):
            return json.dumps(
                {
                    "schema_version": "extension_descriptor_v1",
                    "extension_id": "bad.extension",
                    "release_version": "1.0.0",
                    "identity": {"display_name": "Bad", "description": "", "tags": []},
                    "vendor": {"vendor_name": "Acme"},
                    "distribution": {"channel": "private_byo", "execution_modes": ["remote_https"]},
                    "runtime": {"contract_version": "extension_host_v2"},
                    "artifacts": {"workflows": [{"workflow_id": "wf", "ref": "missing.yaml"}]},
                }
            )

        monkeypatch.setattr("core.application.extensions.lifecycle._fetch_url_text", _fetch)
        monkeypatch.setattr(
            "core.application.extensions.lifecycle._probe_url_exists",
            lambda *_args, **_kwargs: True,
        )

        _raises_input_2992_1 = ExtensionDescriptorImportUrlInput(
            descriptor_url="https://example.com/descriptor.json"
        )
        with pytest.raises(ServiceValidationError):
            cmd_svc.import_descriptor_url(_raises_input_2992_1, scope=_ADMIN_SCOPE)

    def test_install_release_records_failed_handshake_and_projections(self, tmp_path, monkeypatch):
        cmd_svc, query_svc, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path, execution_modes=["remote_https"], with_remote=True
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        attempted = _stub_unreachable_remote_runtime(monkeypatch)

        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com/base",
            ),
            scope=_ADMIN_SCOPE,
        )
        detail = query_svc.get_install_detail(installed.install_id, scope=_ADMIN_SCOPE)

        assert detail.install_id == installed.install_id
        assert detail.handshake_status == "failed"
        assert detail.health_status == "failed"
        assert len(detail.projections) >= 1
        assert attempted == [
            "https://runtime.example.com/base/handshake",
            "https://runtime.example.com/base/health",
        ]

    def test_install_release_rolls_back_partial_state_when_projection_fails(self, tmp_path):
        cmd_svc, _, store, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        original_upsert_projection = runtime_store.upsert_projection

        def _boom(*args, **kwargs):
            raise RuntimeError("projection write failed")

        runtime_store.upsert_projection = _boom
        try:
            _raises_input_3047_1 = ExtensionReleaseInstallInput(
                release_id=imported.release_id, runtime_mode="local"
            )
            with pytest.raises(RuntimeError, match="projection write failed"):
                cmd_svc.install_release(_raises_input_3047_1, scope=_ADMIN_SCOPE)
        finally:
            runtime_store.upsert_projection = original_upsert_projection

        assert store.get(extension_id=imported.extension_id, account_id="acc_default") is None
        release = runtime_store.get_release(imported.release_id)
        assert release is not None
        assert release.extension_id == imported.extension_id
        assert runtime_store.list_entitlements(account_id="acc_default") == []

    def test_install_release_compensates_cancelled_direct_mutation(self):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord

        cmd_svc, _, store, runtime_store, _ = _make_services(
            bl=SimpleNamespace(conn=None, db_store=None, dsn="")
        )
        release_id = "release-cancelled-direct-install"
        extension_id = "acme.cancelled-direct-install"
        runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id=release_id,
                extension_id=extension_id,
                release_version="1.0.0",
                descriptor={"distribution": {"execution_modes": ["local"]}},
                execution_modes=["local"],
                account_id=DEFAULT_ACCOUNT_ID,
            )
        )
        cancellation = asyncio.CancelledError("cancel direct install")
        cmd_svc._install.run_install_handshake = MagicMock(side_effect=cancellation)

        _raises_input_3084_1 = ExtensionReleaseInstallInput(
            release_id=release_id, runtime_mode="local"
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            cmd_svc.install_release(_raises_input_3084_1, scope=_ADMIN_SCOPE)

        assert caught.value is cancellation
        assert store.get(extension_id=extension_id, account_id=DEFAULT_ACCOUNT_ID) is None
        assert runtime_store.list_entitlements(account_id=DEFAULT_ACCOUNT_ID) == []

    @pytest.mark.parametrize("same_transaction", [True, False])
    def test_failed_install_restores_every_mutated_participant_after_transaction_exit(
        self, same_transaction
    ):
        from core.application.extensions.extension_runtime import (
            ExtensionArtifactSnapshotRecord,
            ExtensionReleaseRecord,
        )
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore

        class _Connection:
            def __init__(self):
                self.autocommit = False
                self.rollback_calls = 0
                self.commit_calls = 0
                self.rollback_callbacks = []

            def rollback(self):
                self.rollback_calls += 1
                for callback in self.rollback_callbacks:
                    callback()

            def commit(self):
                self.commit_calls += 1

        conn = _Connection()

        class _InstallStore(ExtensionInstallStore):
            def __init__(self):
                super().__init__()
                self._baseline = copy.deepcopy(self._records)

            def participates_in_transaction(self, transaction_connection):
                return same_transaction and transaction_connection is conn

            def restore_transaction_baseline(self):
                self._records = copy.deepcopy(self._baseline)

            def uninstall(self, *, extension_id, account_id):
                if conn.rollback_calls == 0:
                    raise AssertionError("install compensation ran before transaction rollback")
                return super().uninstall(extension_id=extension_id, account_id=account_id)

        class _RuntimeStore(ExtensionRuntimeStore):
            def participates_in_transaction(self, transaction_connection):
                return same_transaction and transaction_connection is conn

            def capture_transaction_baseline(self):
                self._baseline = copy.deepcopy(self.__dict__)

            def restore_transaction_baseline(self):
                baseline = copy.deepcopy(self._baseline)
                self.__dict__.clear()
                self.__dict__.update(baseline)

            def delete_entitlement(self, install_id):
                if conn.rollback_calls == 0:
                    raise AssertionError("runtime compensation ran before transaction rollback")
                return super().delete_entitlement(install_id)

            def delete_projections_for_install(self, install_id):
                if conn.rollback_calls == 0:
                    raise AssertionError("runtime compensation ran before transaction rollback")
                return super().delete_projections_for_install(install_id)

        class _RegistryStore:
            def __init__(self):
                self.datasets = {}
                self.templates = {}
                self.transaction_connection = None
                self._baseline = ({}, {})

            def _set_tx_conn(self, transaction_connection):
                self.transaction_connection = transaction_connection

            def participates_in_transaction(self, transaction_connection):
                return same_transaction and self.transaction_connection is transaction_connection

            def restore_transaction_baseline(self):
                datasets, templates = self._baseline
                self.datasets = copy.deepcopy(datasets)
                self.templates = copy.deepcopy(templates)

            def get_dataset(self, dataset_id, include_trashed=False):
                del include_trashed
                return copy.deepcopy(self.datasets.get(dataset_id))

            def save_dataset(self, payload, author="admin"):
                del author
                self.datasets[payload["dataset_id"]] = copy.deepcopy(payload)

            def get_dataset_query_template(self, query_template_id, include_trashed=False):
                del include_trashed
                return copy.deepcopy(self.templates.get(query_template_id))

            def save_dataset_query_template(self, payload, author="admin"):
                del payload, author
                raise RuntimeError("registry bridge failed after partial write")

            def list_datasets(self, include_trashed=False):
                del include_trashed
                return [copy.deepcopy(row) for row in self.datasets.values()]

            def list_dataset_query_templates(self, include_trashed=False):
                del include_trashed
                return [copy.deepcopy(row) for row in self.templates.values()]

            def purge_dataset(self, dataset_id):
                if conn.rollback_calls == 0:
                    raise AssertionError("registry compensation ran before transaction rollback")
                self.datasets.pop(dataset_id, None)

            def purge_dataset_query_template(self, query_template_id):
                if conn.rollback_calls == 0:
                    raise AssertionError("registry compensation ran before transaction rollback")
                self.templates.pop(query_template_id, None)

        release_id = "release-transaction-participants"
        extension_id = "acme.transaction-participants"
        runtime_store = _RuntimeStore()
        runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id=release_id,
                extension_id=extension_id,
                release_version="1.0.0",
                descriptor={"distribution": {"execution_modes": ["local"]}},
                execution_modes=["local"],
                account_id=DEFAULT_ACCOUNT_ID,
            )
        )
        runtime_store.replace_artifact_snapshots(
            release_id,
            [
                ExtensionArtifactSnapshotRecord(
                    release_id=release_id,
                    artifact_kind="dataset",
                    artifact_id="orders",
                    snapshot={"dataset_id": "orders", "display_name": "Orders"},
                ),
                ExtensionArtifactSnapshotRecord(
                    release_id=release_id,
                    artifact_kind="saved_query",
                    artifact_id="open_orders",
                    snapshot={
                        "query_template_id": "open_orders",
                        "dataset_id": "orders",
                        "display_name": "Open orders",
                    },
                ),
            ],
        )
        runtime_store.capture_transaction_baseline()
        install_store = _InstallStore()
        registry_store = _RegistryStore()
        if same_transaction:
            conn.rollback_callbacks.extend(
                [
                    install_store.restore_transaction_baseline,
                    runtime_store.restore_transaction_baseline,
                    registry_store.restore_transaction_baseline,
                ]
            )
        service = ExtensionCommandService(
            get_logger=lambda: SimpleNamespace(
                conn=conn,
                db_store=registry_store,
                dsn="",
            ),
            get_install_store=lambda: install_store,
            get_runtime_store=lambda: runtime_store,
        )
        service.run_install_handshake = MagicMock(return_value=None)

        _raises_input_3268_1 = ExtensionReleaseInstallInput(
            release_id=release_id, runtime_mode="local"
        )
        with pytest.raises(
            ServiceUnavailableError,
            match="Extension lifecycle persistence must use one database connection",
        ):
            service.install_release(_raises_input_3268_1, scope=_ADMIN_SCOPE)

        assert conn.rollback_calls == 0
        assert install_store.get(extension_id=extension_id, account_id=DEFAULT_ACCOUNT_ID) is None
        assert runtime_store.list_entitlements(account_id=DEFAULT_ACCOUNT_ID) == []
        assert runtime_store.list_projections(account_id=DEFAULT_ACCOUNT_ID) == []
        assert runtime_store.get_release(release_id) is not None
        assert registry_store.datasets == {}
        assert registry_store.templates == {}

    def test_successful_repoint_retains_previous_release_for_safe_deferred_gc(self):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord

        cmd_svc, _, store, runtime_store, _ = _make_services(
            bl=SimpleNamespace(conn=None, db_store=None, dsn="")
        )
        extension_id = "acme.safe-release-retention"
        old_release_id = "release-safe-retention-old"
        new_release_id = "release-safe-retention-new"
        for release_id, release_version in (
            (old_release_id, "1.0.0"),
            (new_release_id, "2.0.0"),
        ):
            runtime_store.create_release(
                ExtensionReleaseRecord(
                    release_id=release_id,
                    extension_id=extension_id,
                    release_version=release_version,
                    descriptor={"distribution": {"execution_modes": ["local"]}},
                    execution_modes=["local"],
                    account_id=DEFAULT_ACCOUNT_ID,
                )
            )
        previous = store.install(
            extension_id=extension_id,
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=old_release_id,
            runtime_mode="local",
        )

        result = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=new_release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        assert result.install_id == previous.install_id
        assert result.release_id == new_release_id
        assert runtime_store.get_release(old_release_id) is not None
        assert runtime_store.get_release(new_release_id) is not None

    def test_failed_upgrade_re_materializes_previous_release_bridges(self, tmp_path):
        cmd_svc, _, store, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = store.install(
            extension_id=imported.extension_id,
            version="1.0.0",
            account_id="acc_default",
            visibility="account",
            installed_by="test",
            release_id=imported.release_id,
        )
        previous = cmd_svc.install_record_payload(installed)
        materialize = MagicMock()
        cmd_svc._install.materialize_dataset_execution_bridges = materialize

        cmd_svc.rollback_install_release_failure(
            install_store=store,
            runtime_store=runtime_store,
            extension_id=imported.extension_id,
            account_id="acc_default",
            failed_install_id=installed.install_id,
            previous=previous,
            previous_release=runtime_store.get_release(imported.release_id),
        )

        materialize.assert_called_once()
        assert materialize.call_args.kwargs["install_record"].install_id == installed.install_id

    def test_bridge_cleanup_reuses_the_lifecycle_transaction_connection(self):
        from core.application.extensions.command_context import ExtensionCommandContext
        from core.application.extensions.command_install_projection import (
            ExtensionInstallProjectionService,
        )

        install_id = "install-bridge"
        lifecycle_connection = object()
        registry_store = MagicMock()
        registry_store.list_dataset_query_templates.return_value = [
            {
                "query_template_id": "extension.install-bridge.query",
                "metadata_jsonb": {"projection_bridge": {"install_id": install_id}},
            }
        ]
        registry_store.list_datasets.return_value = [
            {
                "dataset_id": "extension.install-bridge.dataset",
                "metadata_jsonb": {"projection_bridge": {"install_id": install_id}},
            }
        ]
        bl = MagicMock()
        bl.conn = lifecycle_connection
        bl.db_store = registry_store
        projection_service = ExtensionInstallProjectionService(
            context=ExtensionCommandContext(get_logger=lambda: bl)
        )

        projection_service.remove_dataset_bridges(install_id=install_id)

        assert [call.args for call in registry_store._set_tx_conn.call_args_list] == [
            (lifecycle_connection,),
            (None,),
        ]
        registry_store.purge_dataset_query_template.assert_called_once_with(
            "extension.install-bridge.query"
        )
        registry_store.purge_dataset.assert_called_once_with("extension.install-bridge.dataset")

    def test_import_descriptor_file_install_after_import_cleans_release_on_install_failure(
        self, tmp_path
    ):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        original_install_release = cmd_svc._install.install_release

        def _fail(*args, **kwargs):
            raise RuntimeError("install failed")

        cmd_svc._install.install_release = _fail
        try:
            _raises_input_3409_1 = ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path), install_after_import=True
            )
            with pytest.raises(RuntimeError, match="install failed"):
                cmd_svc.import_descriptor_file(_raises_input_3409_1, scope=_ADMIN_SCOPE)
        finally:
            cmd_svc._install.install_release = original_install_release

        releases = runtime_store.list_releases(
            extension_id="acme.runtime-extension", account_id="acc_default"
        )
        assert releases == []

    def test_install_release_rejects_same_version_update(self, tmp_path):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_3437_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="already installed"):
            cmd_svc.install_release(_raises_input_3437_1, scope=_ADMIN_SCOPE)

    def test_install_release_rejects_downgrade_update(self, tmp_path):
        cmd_svc, _, store, _, _ = _make_services()
        descriptor_v1 = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported_v1 = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_v1)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported_v1.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        descriptor_v2 = descriptor_v1
        descriptor_v2_payload = json.loads(descriptor_v2.read_text(encoding="utf-8"))
        descriptor_v2_payload["release_version"] = "1.0.1"
        descriptor_v2.write_text(json.dumps(descriptor_v2_payload), encoding="utf-8")
        imported_v2 = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_v2)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported_v2.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        descriptor_v2_payload["release_version"] = "1.0.0"
        descriptor_v1.write_text(json.dumps(descriptor_v2_payload), encoding="utf-8")
        imported_v1_again = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_v1)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_3474_1 = ExtensionReleaseInstallInput(
            release_id=imported_v1_again.release_id, runtime_mode="local"
        )
        with pytest.raises(ServiceValidationError, match="Downgrade not supported"):
            cmd_svc.install_release(_raises_input_3474_1, scope=_ADMIN_SCOPE)

        current = store.get(extension_id="acme.runtime-extension", account_id=DEFAULT_ACCOUNT_ID)
        assert current is not None
        assert current.version == "1.0.1"

    def test_install_release_upgrade_invalidates_existing_llm_models(self, tmp_path, monkeypatch):
        from core.application.settings import llm_provider_extension_repository as llm_store

        cmd_svc, _, _, _, bl = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported_v1 = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported_v1.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        payload["release_version"] = "1.0.1"
        descriptor_path.write_text(json.dumps(payload), encoding="utf-8")
        imported_v2 = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        invalidated: list[dict[str, Any]] = []
        monkeypatch.setattr(
            llm_store,
            "mark_install_models_stale",
            lambda conn, **kwargs: invalidated.append({"conn": conn, **kwargs}) or 1,
        )

        cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported_v2.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        assert invalidated == [
            {
                "conn": bl.conn,
                "account_id": DEFAULT_ACCOUNT_ID,
                "extension_install_id": installed.install_id,
            }
        ]

    def test_install_release_uses_membership_install_provider_lock_order(self, monkeypatch):
        from core.application.extensions.extension_runtime import ExtensionReleaseRecord
        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore
        from core.application.settings import llm_provider_extension_repository as llm_store

        events: list[str] = []

        class OrderedInstallStore(ExtensionInstallStore):
            def lock_release_membership(self, release_id: str) -> None:
                events.append(f"membership:{release_id}")

            def get(self, *, extension_id: str, account_id: str):
                events.append("install:get")
                return super().get(extension_id=extension_id, account_id=account_id)

            def install(self, **kwargs):
                events.append("install:upsert")
                return super().install(**kwargs)

        class OrderedRuntimeStore(ExtensionRuntimeStore):
            def get_release(self, release_id: str):
                events.append(f"release:{release_id}")
                return super().get_release(release_id)

        install_store = OrderedInstallStore()
        runtime_store = OrderedRuntimeStore()
        extension_id = "fixture.ordered-repoint"
        old_release_id = "release-old"
        target_release_id = "release-target"
        descriptor = {
            "distribution": {"execution_modes": ["local"]},
            "runtime": {"contract_version": "extension_host_v2"},
        }
        runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id=old_release_id,
                extension_id=extension_id,
                release_version="1.0.0",
                descriptor=descriptor,
                source_kind="extension_scan",
            )
        )
        runtime_store.create_release(
            ExtensionReleaseRecord(
                release_id=target_release_id,
                extension_id=extension_id,
                release_version="2.0.0",
                descriptor=descriptor,
                source_kind="extension_scan",
            )
        )
        existing = install_store.install(
            extension_id=extension_id,
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=old_release_id,
            descriptor_source="extension_scan",
            runtime_mode="local",
        )
        events.clear()
        bl = MagicMock()
        bl.conn = object()
        bl.db_store = None
        bl.dsn = ""
        service, _, _, _, _ = _make_services(
            store=install_store,
            runtime_store=runtime_store,
            bl=bl,
        )
        service._install.run_install_handshake = MagicMock(return_value=None)
        monkeypatch.setattr(
            llm_store,
            "mark_install_models_stale",
            lambda _conn, **_kwargs: events.append("provider:invalidate") or 1,
        )
        monkeypatch.setattr(
            llm_store,
            "set_install_provider_state_revision",
            lambda _conn, **_kwargs: True,
        )

        service._install._install_release(
            ExtensionReleaseInstallInput(
                release_id=target_release_id,
                runtime_mode="local",
            ),
            scope=_ADMIN_SCOPE,
        )

        assert existing.install_id
        ordered_events = [
            event
            for event in events
            if event
            in {
                f"membership:{target_release_id}",
                f"release:{target_release_id}",
                "install:get",
                "install:upsert",
                "provider:invalidate",
            }
        ]
        assert ordered_events == [
            f"membership:{target_release_id}",
            f"release:{target_release_id}",
            "install:get",
            "install:upsert",
            "provider:invalidate",
        ], events

    def test_install_release_rejects_unsafe_remote_base_url_override(self, tmp_path):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path, execution_modes=["remote_https"], with_remote=True
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_3648_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id,
            runtime_mode="remote_https",
            remote_base_url="http://127.0.0.1:9999/internal",
        )
        with pytest.raises(ServiceValidationError, match="remote_base_url"):
            cmd_svc.install_release(_raises_input_3648_1, scope=_ADMIN_SCOPE)

    def test_handshake_marks_incompatible_when_platform_max_exceeded(self, tmp_path, monkeypatch):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        descriptor_payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        # The maximum is the current host contract (also the descriptor's minimum,
        # so the range stays valid); a later host then exceeds it.
        compatibility = descriptor_payload["runtime"]["compatibility"]
        compatibility["platform_max"] = compatibility["platform_min"]
        descriptor_path.write_text(json.dumps(descriptor_payload), encoding="utf-8")
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        monkeypatch.setenv("FS_VERSION", "9.0.0")
        health = cmd_svc.run_install_handshake(
            install_id=installed.install_id,
            scope=_ADMIN_SCOPE,
        )
        assert health.runtime_status == "incompatible"
        assert "supported maximum" in health.last_error

    def test_handshake_does_not_probe_unsafe_stored_remote_base_url(self, tmp_path, monkeypatch):
        cmd_svc, _, install_store, _, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path, execution_modes=["remote_https"], with_remote=True
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        _stub_unreachable_remote_runtime(monkeypatch)
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com/base",
            ),
            scope=_ADMIN_SCOPE,
        )
        install_store.set_runtime_state(
            install_id=installed.install_id,
            remote_base_url="http://127.0.0.1:9999/internal",
        )

        # remote_handshake turns any exception into "upstream_error", so the raise
        # alone cannot fail the test; the recorded URLs prove nothing was opened.
        probed: list[str] = []

        def _must_not_open(request, **_kwargs):
            probed.append(request.full_url)
            raise AssertionError("urlopen must not be called")

        monkeypatch.setattr(
            "core.application.extensions.url_safety.open_pinned_url", _must_not_open
        )
        health = cmd_svc.run_install_handshake(install_id=installed.install_id, scope=_ADMIN_SCOPE)
        assert health.handshake_status == "failed"
        assert health.health_status == "failed"
        assert health.last_error == "Handshake failed: upstream_error"
        assert probed == []

    def test_handshake_uses_current_rotated_llm_runtime_credential_ref(self, tmp_path, monkeypatch):
        from core.application.auth import secrets as secret_crypto
        from core.application.settings import llm_provider_extension_repository as llm_store
        from core.infrastructure.db import remote_extension_runtime

        cmd_svc, _, install_store, _, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path, execution_modes=["remote_https"], with_remote=True
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        monkeypatch.setattr(
            llm_store,
            "get_install_current_credential_snapshot",
            lambda *_args, **_kwargs: (
                "llmext_cred_install_old",
                '{"runtime_bearer_token":"runtime-old"}',
            ),
        )
        monkeypatch.setattr(
            llm_store,
            "get_install_runtime_credential_snapshot",
            lambda *_args, **_kwargs: {
                "release_id": imported.release_id,
                "runtime_mode": "remote_https",
                "remote_base_url": "https://runtime-rotated.example/base",
                "credential_secret_ref": "llmext_cred_install_rotated",
                "encrypted_value": '{"runtime_bearer_token":"runtime-rotated"}',
            },
            raising=False,
        )
        monkeypatch.setattr(secret_crypto, "decrypt_secret", lambda value: value)
        seen_snapshots = []
        monkeypatch.setattr(
            remote_extension_runtime,
            "remote_handshake",
            lambda extension, **_kwargs: (
                seen_snapshots.append(
                    (extension.get("remote_base_url"), extension.get("remote_bearer_token"))
                )
                or {"status": "succeeded"}
            ),
        )
        monkeypatch.setattr(
            remote_extension_runtime,
            "remote_health",
            lambda extension, **_kwargs: (
                seen_snapshots.append(
                    (extension.get("remote_base_url"), extension.get("remote_bearer_token"))
                )
                or {"status": "succeeded"}
            ),
        )

        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com/base",
            ),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.run_install_handshake(install_id=installed.install_id, scope=_ADMIN_SCOPE)

        assert seen_snapshots
        assert set(seen_snapshots) == {("https://runtime-rotated.example/base", "runtime-rotated")}
        persisted = install_store.get_by_install_id(installed.install_id)
        assert persisted is not None
        assert persisted.remote_base_url == "https://runtime-rotated.example/base"

    def _installed_remote(self, tmp_path, monkeypatch):
        cmd_svc, _, install_store, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(
            tmp_path, execution_modes=["remote_https"], with_remote=True
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        _stub_unreachable_remote_runtime(monkeypatch)
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com/base",
            ),
            scope=_ADMIN_SCOPE,
        )
        return cmd_svc, install_store, runtime_store, imported, installed

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "ok"},
            {"status": "ok", "contract_version": "extension_host_v1"},
            # The token alone does not confirm the final execution scope.
            {"status": "ok", "contract_version": "extension_host_v2"},
            {
                "status": "ok",
                "contract_version": "extension_host_v2",
                "execution_scope_fields": ["group_id", "step_id", "step_execution_id"],
            },
        ],
        ids=["missing-confirmation", "foreign-contract", "token-only", "three-field-scope"],
    )
    def test_handshake_without_v2_confirmation_marks_install_incompatible(
        self, tmp_path, monkeypatch, result
    ):
        from core.application.extensions.remote_runtime_protocol_v2 import RemoteRuntimeClient
        from core.infrastructure.db import remote_extension_runtime
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_query

        cmd_svc, install_store, _, _, installed = self._installed_remote(tmp_path, monkeypatch)
        calls: list[tuple[str, str, dict]] = []

        def _http_call(method, url, _headers, payload, _timeout):
            calls.append((method, url, dict(payload)))
            if url.endswith("/handshake"):
                return 200, {"ok": True, "result": dict(result)}
            return 200, {"ok": True, "result": {"status": "healthy"}}

        monkeypatch.setattr(
            remote_extension_runtime,
            "RemoteRuntimeClient",
            lambda **kwargs: RemoteRuntimeClient(**kwargs, http_call=_http_call),
        )

        health = cmd_svc.run_install_handshake(install_id=installed.install_id, scope=_ADMIN_SCOPE)

        assert health.runtime_status == "incompatible"
        assert health.handshake_status == "failed"
        assert "extension_host_v2" in health.last_error
        # One negotiation naming v2; no health check stands in for it, no v1 retry.
        assert [(method, url.rsplit("/", 1)[-1]) for method, url, _ in calls] == [
            ("POST", "handshake")
        ]
        assert calls[0][2]["contract_version"] == "extension_host_v2"
        persisted = install_store.get_by_install_id(installed.install_id)
        assert persisted.runtime_status == "incompatible"

        # The install is refused before any operation is dispatched.
        calls.clear()
        query = run_extension_query(
            {
                "extension_key": "acme.runtime-extension",
                "runtime_mode": "remote_https",
                "remote_base_url": "https://runtime.example.com/base",
                "runtime_status": persisted.runtime_status,
                "handshake_status": persisted.handshake_status,
                "health_status": persisted.health_status,
            },
            query_id="list_accounts",
        )
        assert query["status"] == "failed"
        assert calls == []

    def test_handshake_confirming_v2_is_compatible(self, tmp_path, monkeypatch):
        from core.application.extensions.remote_runtime_protocol_v2 import RemoteRuntimeClient
        from core.infrastructure.db import remote_extension_runtime

        cmd_svc, _, _, _, installed = self._installed_remote(tmp_path, monkeypatch)
        paths: list[str] = []

        def _http_call(method, url, _headers, payload, _timeout):
            paths.append(url.rsplit("/", 1)[-1])
            if url.endswith("/handshake"):
                assert payload["contract_version"] == "extension_host_v2"
                return 200, {
                    "ok": True,
                    "result": {
                        "contract_version": "extension_host_v2",
                        "execution_scope_fields": [
                            "group_id",
                            "for_each_id",
                            "step_id",
                            "step_execution_id",
                        ],
                    },
                }
            return 200, {"ok": True, "result": {"status": "healthy"}}

        monkeypatch.setattr(
            remote_extension_runtime,
            "RemoteRuntimeClient",
            lambda **kwargs: RemoteRuntimeClient(**kwargs, http_call=_http_call),
        )

        health = cmd_svc.run_install_handshake(install_id=installed.install_id, scope=_ADMIN_SCOPE)

        assert health.runtime_status == "compatible"
        assert health.handshake_status == "ok"
        assert health.health_status == "ok"
        assert paths == ["handshake", "health"]

    def test_release_of_another_contract_is_incompatible_without_any_call(
        self, tmp_path, monkeypatch
    ):
        from core.infrastructure.db import remote_extension_runtime

        cmd_svc, _, runtime_store, imported, installed = self._installed_remote(
            tmp_path, monkeypatch
        )
        release = runtime_store.get_release(imported.release_id)
        monkeypatch.setattr(
            runtime_store,
            "get_release",
            lambda _release_id: release.model_copy(
                update={"contract_version": "extension_host_v1"}
            ),
        )
        monkeypatch.setattr(
            remote_extension_runtime,
            "remote_handshake",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not call the runtime")),
        )
        monkeypatch.setattr(
            remote_extension_runtime,
            "remote_health",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not call the runtime")),
        )

        health = cmd_svc.run_install_handshake(install_id=installed.install_id, scope=_ADMIN_SCOPE)

        assert health.runtime_status == "incompatible"
        assert health.handshake_status == "failed"
        assert "extension_host_v1" in health.last_error

    def test_descriptor_of_another_host_contract_is_refused(self, tmp_path):
        from core.application.extensions.extension_descriptor import validate_extension_descriptor

        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        assert validate_extension_descriptor(descriptor)["valid"] is True
        descriptor["runtime"]["contract_version"] = "extension_host_v1"

        result = validate_extension_descriptor(descriptor)

        assert result["valid"] is False
        assert "extension_host_v2" in json.dumps(result["errors"])

    def test_http_probe_rejects_redirect_to_private_host(self, monkeypatch):
        import urllib.error

        from core.application.extensions.service_support import _http_probe

        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )

        def _redirecting_open(_request, *, timeout_seconds, purpose=""):
            _ = (timeout_seconds, purpose)
            raise urllib.error.HTTPError(
                url="https://runtime.example.com/health",
                code=302,
                msg="Found",
                hdrs={"Location": "http://127.0.0.1/internal"},
                fp=None,
            )

        monkeypatch.setattr(
            "core.application.extensions.url_safety.open_pinned_url",
            _redirecting_open,
        )

        ok, detail = _http_probe("https://runtime.example.com/health")

        assert ok is False
        assert "127.0.0.1" in detail
        assert "not allowed" in detail

    def test_runtime_token_denied_for_suspended_or_incompatible_install(self, tmp_path):
        cmd_svc, _, install_store, runtime_store, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        runtime_store.upsert_entitlement(
            install_id=installed.install_id,
            account_id=installed.account_id,
            status="suspended_nonpayment",
            updated_by="checker",
        )
        denied = cmd_svc.issue_runtime_token(
            ExtensionRuntimeTokenInput(install_id=installed.install_id, ttl_seconds=300),
            scope=_ADMIN_SCOPE,
        )
        assert denied.issued is False
        assert "Entitlement status" in denied.reason

        runtime_store.upsert_entitlement(
            install_id=installed.install_id,
            account_id=installed.account_id,
            status="active",
            updated_by="checker",
        )
        install_store.set_runtime_state(
            install_id=installed.install_id,
            runtime_status="incompatible",
        )
        denied_incompatible = cmd_svc.issue_runtime_token(
            ExtensionRuntimeTokenInput(install_id=installed.install_id, ttl_seconds=300),
            scope=_ADMIN_SCOPE,
        )
        assert denied_incompatible.issued is False
        assert "incompatible" in denied_incompatible.reason.lower()

    def test_runtime_token_issued_with_valid_connection(self, tmp_path):
        bl = MagicMock()
        cursor_inner = MagicMock()
        cursor_inner.fetchall.return_value = [
            {
                "connection_id": "conn_rt_1",
                "project_id": 1,
                "extension_id": None,
                "account_id": "acc_default",
                "connection_kind": "extension_runtime",
                "extension_install_id": "pi_runtime_1",
                "runtime_mode": "local",
                "remote_base_url": "",
                "health_status": "ok",
                "runtime_status": "compatible",
                "last_seen_ok_at": None,
                "last_error": "",
                "enabled": True,
                "connection_config": {},
                "secret_refs": [],
                "connection_routes": [],
                "ingress_config": {},
                "workflow_mappings": {},
                "created_at": None,
                "updated_at": None,
            }
        ]
        cursor_ctx = MagicMock()
        cursor_ctx.__enter__ = MagicMock(return_value=cursor_inner)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        bl.cursor = MagicMock(return_value=cursor_ctx)
        bl.get_project.return_value = {"id": "1", "account_id": DEFAULT_ACCOUNT_ID}

        cmd_svc, _, install_store, runtime_store, _ = _make_services(bl=bl)
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        runtime_store.upsert_entitlement(
            install_id=installed.install_id,
            account_id=installed.account_id,
            status="active",
            updated_by="checker",
        )
        install_store.set_runtime_state(
            install_id=installed.install_id,
            runtime_status="compatible",
            health_status="ok",
            handshake_status="ok",
        )
        with patch.dict(
            "os.environ",
            {"FS_EXTENSION_RUNTIME_SIGNING_SECRET": "test-runtime-secret"},
            clear=False,
        ):
            token = cmd_svc.issue_runtime_token(
                ExtensionRuntimeTokenInput(
                    install_id=installed.install_id,
                    connection_id="conn_rt_1",
                    project_id=1,
                    ttl_seconds=600,
                ),
                scope=_ADMIN_SCOPE,
            )
        assert token.issued is True
        assert token.token.count(".") == 2

    def test_runtime_token_rejects_project_outside_scope_allowlist(self, tmp_path):
        bl = MagicMock()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        cmd_svc, _, _install_store, _runtime_store, _ = _make_services(bl=bl)
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )

        _raises_input_3954_1 = ExtensionRuntimeTokenInput(
            install_id=installed.install_id,
            connection_id="conn_rt_1",
            project_id=7,
            ttl_seconds=600,
        )
        _raises_input_3954_2 = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin-restricted",
            principal_type="admin",
            allowed_project_ids=frozenset({"99"}),
        )
        with pytest.raises(ServiceForbiddenError):
            cmd_svc.issue_runtime_token(_raises_input_3954_1, scope=_raises_input_3954_2)

    def test_runtime_token_denied_without_configured_signing_secret(self, tmp_path):
        bl = MagicMock()
        cursor_inner = MagicMock()
        cursor_inner.fetchall.return_value = [
            {
                "connection_id": "conn_rt_1",
                "project_id": 1,
                "extension_id": None,
                "account_id": "acc_default",
                "connection_kind": "extension_runtime",
                "extension_install_id": "pi_runtime_1",
                "runtime_mode": "local",
                "remote_base_url": "",
                "health_status": "ok",
                "runtime_status": "compatible",
                "last_seen_ok_at": None,
                "last_error": "",
                "enabled": True,
                "connection_config": {},
                "secret_refs": [],
                "connection_routes": [],
                "ingress_config": {},
                "workflow_mappings": {},
                "created_at": None,
                "updated_at": None,
            }
        ]
        cursor_ctx = MagicMock()
        cursor_ctx.__enter__ = MagicMock(return_value=cursor_inner)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        bl.cursor = MagicMock(return_value=cursor_ctx)
        bl.get_project.return_value = {"id": "1", "account_id": DEFAULT_ACCOUNT_ID}

        cmd_svc, _, install_store, runtime_store, _ = _make_services(bl=bl)
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=_ADMIN_SCOPE,
        )
        runtime_store.upsert_entitlement(
            install_id=installed.install_id,
            account_id=installed.account_id,
            status="active",
            updated_by="checker",
        )
        install_store.set_runtime_state(
            install_id=installed.install_id,
            runtime_status="compatible",
            health_status="ok",
            handshake_status="ok",
        )
        with patch.dict(
            "os.environ",
            {
                "FS_EXTENSION_RUNTIME_SIGNING_SECRET": "",
                "FS_SECRET_KEY": "",
                "SECRET_KEY": "",
            },
            clear=False,
        ):
            denied = cmd_svc.issue_runtime_token(
                ExtensionRuntimeTokenInput(
                    install_id=installed.install_id,
                    connection_id="conn_rt_1",
                    project_id=1,
                    ttl_seconds=600,
                ),
                scope=_ADMIN_SCOPE,
            )
        assert denied.issued is False
        assert "signing secret is not configured" in denied.reason

    def test_install_release_invalid_entitlement_status_returns_validation_error(self, tmp_path):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = self._write_descriptor(tmp_path, execution_modes=["local"])
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        invalid = ExtensionReleaseInstallInput.model_construct(
            release_id=imported.release_id,
            runtime_mode="local",
            remote_base_url="",
            entitlement_status="definitely_bad",
        )
        with pytest.raises(ServiceValidationError, match="entitlement_status"):
            cmd_svc.install_release(invalid, scope=_ADMIN_SCOPE)

    def test_generated_job_create_persists_private_record(self):
        cmd_svc, _, _, runtime_store, _ = _make_services()
        created = cmd_svc.create_generated_extension_job(
            ExtensionGeneratedJobCreateInput(
                source_job_id=42,
                request_payload={"prompt": "Generate private runtime extension"},
                visibility="private",
            ),
            scope=_ADMIN_SCOPE,
        )
        assert created.status == "created"
        saved = runtime_store.get_generated_extension_job(created.generated_job_id)
        assert saved is not None
        assert saved.visibility == "private"
        assert saved.source_job_id == "42"


class TestExtensionLifecycleAdminGate:
    """Descriptor import and runtime-token issuance require an admin role."""

    _OPERATOR_SCOPE = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID, actor_id="op", principal_type="operator"
    )

    def test_import_descriptor_file_rejects_operator(self, tmp_path):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )
        _raises_input_4385_1 = ExtensionDescriptorImportFileInput(
            descriptor_path=str(descriptor_path)
        )
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            cmd_svc.import_descriptor_file(_raises_input_4385_1, scope=self._OPERATOR_SCOPE)

    def test_import_descriptor_file_allows_admin(self, tmp_path):
        cmd_svc, _, _, _, _ = _make_services()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )
        result = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        assert result.status == "imported"

    def test_import_descriptor_url_rejects_operator(self):
        cmd_svc, _, _, _, _ = _make_services()
        _raises_input_4404_1 = ExtensionDescriptorImportUrlInput(
            descriptor_url="https://example.com/extension-descriptor.json"
        )
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            cmd_svc.import_descriptor_url(_raises_input_4404_1, scope=self._OPERATOR_SCOPE)

    def test_issue_runtime_token_rejects_operator(self):
        cmd_svc, _, _, _, _ = _make_services()
        _raises_input_4422_1 = ExtensionRuntimeTokenInput(
            install_id="inst_1", connection_id="conn_1"
        )
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            cmd_svc.issue_runtime_token(_raises_input_4422_1, scope=self._OPERATOR_SCOPE)


class TestExtensionApiHttpContracts:
    def test_legacy_install_route_is_explicitly_retired(self):
        client, store, _runtime_store, _bl = _make_extension_api_client()

        response = client.post(
            "/api/extensions/acme.runtime-extension/install",
            headers={"x-test-role": "viewer"},
            json={"version": "1.0.0", "config": {}},
        )

        assert response.status_code == 422
        assert store.get(extension_id="acme.runtime-extension", account_id="acc_default") is None

    def test_descriptor_import_file_http_contract_and_scope(self, tmp_path):
        client, store, _runtime_store, _bl = _make_extension_api_client()
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )

        response = client.post(
            "/api/extensions/descriptors/import/file",
            headers={"x-test-actor": "checker"},
            json={
                "descriptor_path": str(descriptor_path),
                "install_after_import": True,
                "runtime_mode": "local",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "status",
            "release_id",
            "extension_id",
            "release_version",
            "imported_artifacts",
            "install",
            "errors",
        }
        assert body["status"] == "imported"
        assert body["extension_id"] == "acme.runtime-extension"
        assert body["errors"] == []
        assert isinstance(body["release_id"], str)
        assert body["release_id"].startswith("pr_")
        assert isinstance(body["imported_artifacts"], list)
        assert len(body["imported_artifacts"]) == 2
        assert all(
            set(item) == {"artifact_kind", "artifact_id", "publish"}
            for item in body["imported_artifacts"]
        )
        assert set(body["install"]) == {
            "status",
            "extension_id",
            "version",
            "account_id",
            "install_id",
            "release_id",
            "dependency_warnings",
        }
        assert body["install"]["status"] == "installed"
        assert body["install"]["account_id"] == DEFAULT_ACCOUNT_ID
        assert (
            store.get(extension_id="acme.runtime-extension", account_id=DEFAULT_ACCOUNT_ID)
            is not None
        )

    def test_descriptor_import_file_http_rejects_missing_path(self):
        client, _store, _runtime_store, _bl = _make_extension_api_client()

        response = client.post(
            "/api/extensions/descriptors/import/file",
            json={},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["detail"][0]["loc"] == ["body", "descriptor_path"]
        assert body["detail"][0]["type"] == "missing"

    def test_descriptor_import_file_http_cleans_release_when_install_fails(self, tmp_path):
        runtime_store = _make_runtime_store()
        client, _store, _runtime_store, _bl = _make_extension_api_client(
            runtime_store=runtime_store
        )
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )
        original_upsert_projection = runtime_store.upsert_projection

        def _boom(*args, **kwargs):
            raise RuntimeError("projection write failed")

        runtime_store.upsert_projection = _boom
        try:
            response = client.post(
                "/api/extensions/descriptors/import/file",
                headers={"x-test-actor": "checker"},
                json={
                    "descriptor_path": str(descriptor_path),
                    "install_after_import": True,
                    "runtime_mode": "local",
                },
            )
        finally:
            runtime_store.upsert_projection = original_upsert_projection

        assert response.status_code == 500
        assert (
            runtime_store.list_releases(
                extension_id="acme.runtime-extension", account_id=DEFAULT_ACCOUNT_ID
            )
            == []
        )

    def test_runtime_token_http_contract_and_cross_account_denial(self, tmp_path):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        bl = _make_cursor_logger()
        bl.get_project.return_value = {"id": "7", "account_id": DEFAULT_ACCOUNT_ID}
        client, _store, _runtime_store, _bl = _make_extension_api_client(
            store=store, runtime_store=runtime_store, bl=bl
        )
        cmd_svc, _query_svc, _store2, runtime_store2, _bl2 = _make_services(
            store=store, runtime_store=runtime_store, bl=bl
        )
        descriptor_path = TestExtensionRuntimeFoundation._write_descriptor(
            tmp_path, execution_modes=["local"]
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=ScopeContext(
                principal_type="admin", account_id=DEFAULT_ACCOUNT_ID, actor_id="checker"
            ),
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(release_id=imported.release_id, runtime_mode="local"),
            scope=ScopeContext(
                principal_type="admin", account_id=DEFAULT_ACCOUNT_ID, actor_id="checker"
            ),
        )
        runtime_store2.upsert_entitlement(
            install_id=installed.install_id,
            account_id=DEFAULT_ACCOUNT_ID,
            status="active",
            updated_by="checker",
        )
        store.set_runtime_state(
            install_id=installed.install_id,
            runtime_status="compatible",
            health_status="ok",
            handshake_status="ok",
        )

        with (
            patch(
                "core.application.extensions.command_install._list_extension_runtime_connections",
                return_value=[
                    {
                        "connection_id": "conn_rt_1",
                        "project_id": 7,
                        "connection_kind": "extension_runtime",
                        "extension_install_id": installed.install_id,
                        "runtime_mode": "local",
                        "health_status": "ok",
                        "runtime_status": "compatible",
                        "enabled": True,
                    }
                ],
            ),
            patch.dict(
                "os.environ",
                {"FS_EXTENSION_RUNTIME_SIGNING_SECRET": "test-runtime-secret"},
                clear=False,
            ),
        ):
            response = client.post(
                "/api/extensions/runtime/token",
                headers={},
                json={
                    "install_id": installed.install_id,
                    "connection_id": "conn_rt_1",
                    "project_id": 7,
                    "ttl_seconds": 600,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "issued",
            "token",
            "token_type",
            "install_id",
            "expires_at",
            "ttl_seconds",
            "reason",
        }
        assert body["issued"] is True
        assert body["token_type"] == "Bearer"
        assert body["install_id"] == installed.install_id
        assert body["ttl_seconds"] == 600
        assert body["reason"] == ""
        assert body["token"].count(".") == 2
        assert isinstance(body["expires_at"], float)

        denied = client.post(
            "/api/extensions/runtime/token",
            headers={"x-test-account-id": "acc_beta"},
            json={"install_id": installed.install_id},
        )
        assert denied.status_code == 404
        assert "not found" in denied.json()["error"].lower()

    def test_system_bootstrap_route_is_gone(self):
        """There is no system extension tier, so no route bootstraps one."""
        client, _store, _runtime_store, _bl = _make_extension_api_client()

        response = client.post(
            "/api/extensions/bootstrap/system",
            json={"source_root": "/tmp", "install_after_import": True},
        )

        assert response.status_code in {404, 405}

    def test_generated_jobs_http_contract_and_visibility_rejection(self):
        client, _store, runtime_store, _bl = _make_extension_api_client()

        response = client.post(
            "/api/extensions/generated-jobs",
            headers={"x-test-account-id": "acc_alpha", "x-test-actor": "builder"},
            json={
                "source_job_id": 42,
                "request_payload": {"prompt": "Generate private runtime extension"},
                "visibility": "private",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "status",
            "generated_job_id",
            "account_id",
            "source_job_id",
            "visibility",
        }
        assert body["status"] == "created"
        assert body["account_id"] == "acc_alpha"
        assert body["source_job_id"] == "42"
        assert body["visibility"] == "private"
        saved = runtime_store.get_generated_extension_job(body["generated_job_id"])
        assert saved is not None
        assert saved.account_id == "acc_alpha"

        rejected = client.post(
            "/api/extensions/generated-jobs",
            json={
                "source_job_id": 42,
                "request_payload": {"prompt": "Generate public runtime extension"},
                "visibility": "public",
            },
        )
        assert rejected.status_code == 400
        assert "private" in rejected.json()["error"].lower()


class TestExtensionQueryFilters:
    @staticmethod
    def _write_market_descriptor(
        root,
        *,
        extension_id: str,
        vendor: str,
        category: str,
        tags: list[str],
        execution_modes: list[str],
        channel: str = "marketplace_managed",
        allows_self_hosted: bool = True,
        requires_connected_control_plane: bool = False,
        billing_mode: str = "",
        plan_id: str = "",
        include_agent: bool = True,
        with_remote: bool = False,
    ):
        from core.application.extensions.service_support import _runtime_platform_version

        (root / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
        (root / "artifacts" / "workflows" / "wf.yaml").write_text(
            "workflow_id: wf\n", encoding="utf-8"
        )
        if include_agent:
            (root / "artifacts" / "agents").mkdir(parents=True, exist_ok=True)
            (root / "artifacts" / "agents" / "agent.yaml").write_text(
                "agent_id: agent\n", encoding="utf-8"
            )
        artifacts_payload = {
            "workflows": [
                {"workflow_id": "wf", "ref": "artifacts/workflows/wf.yaml", "publish": True}
            ],
        }
        if include_agent:
            artifacts_payload["agents"] = [
                {"agent_id": "agent", "ref": "artifacts/agents/agent.yaml", "publish": True}
            ]
        descriptor = {
            "schema_version": "extension_descriptor_v1",
            "extension_id": extension_id,
            "release_version": "1.0.0",
            "extension_type": "service_extension",
            "identity": {
                "display_name": f"{extension_id} display",
                "description": f"{extension_id} desc",
                "tags": tags,
            },
            "vendor": {"vendor_name": vendor, "support_url": "https://vendor.example/support"},
            "listing": {"category": category, "visibility": "public"},
            "distribution": {
                "channel": channel,
                "execution_modes": execution_modes,
                "allows_self_hosted": allows_self_hosted,
                "requires_connected_control_plane": requires_connected_control_plane,
                "import_sources": ["url", "file"],
            },
            "runtime": {
                "contract_version": "extension_host_v2",
                "compatibility": {"platform_min": _runtime_platform_version()},
            },
            "artifacts": artifacts_payload,
            "provenance": {"origin": "vendor_published"},
        }
        if billing_mode or plan_id:
            descriptor["commerce"] = {
                "billing_mode": billing_mode,
                "plan_id": plan_id,
            }
        if with_remote:
            descriptor["runtime"]["remote"] = {
                "base_url": "https://runtime.example.com",
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/query/{query_id}",
                    "action": "/action/{action_id}",
                    "emit_event": "/events",
                },
            }
        descriptor_path = root / "extension-descriptor.json"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        return descriptor_path

    def test_installed_list_supports_disabled_state_filter_for_owner(self):
        cmd_svc, query_svc, store, _, _ = _make_services()
        store.install(
            extension_id="owner-extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )
        cmd_svc.disable_extension("owner-extension", scope=_ADMIN_SCOPE)

        all_rows = query_svc.list_extensions(scope=_ADMIN_SCOPE)
        disabled_rows = query_svc.list_extensions(
            filters={"state": ["disabled"]}, scope=_ADMIN_SCOPE
        )

        assert all_rows.total == 1
        assert all_rows.extensions[0].state == "disabled"
        assert disabled_rows.total == 1
        assert disabled_rows.extensions[0].extension_id == "owner-extension"
        assert disabled_rows.extensions[0].state == "disabled"

    def test_status_group_billing_blocked_when_entitlement_suspended_nonpayment(self, tmp_path):
        cmd_svc, query_svc, _, _, _ = _make_services()
        descriptor_path = self._write_market_descriptor(
            tmp_path / "billing-extension",
            extension_id="billing.extension",
            vendor="Acme",
            category="ops",
            tags=["billing"],
            execution_modes=["local"],
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="local",
                entitlement_status="suspended_nonpayment",
            ),
            scope=_ADMIN_SCOPE,
        )

        rows = query_svc.list_extensions(filters={"q": "billing.extension"}, scope=_ADMIN_SCOPE)
        assert rows.total == 1
        assert rows.extensions[0].display_name == "billing.extension display"
        assert rows.extensions[0].vendor_name == "Acme"
        assert rows.extensions[0].category == "ops"
        assert rows.extensions[0].tags == ["billing"]
        assert rows.extensions[0].entitlement_status == "suspended_nonpayment"
        assert rows.extensions[0].status_group == "billing_blocked"

    def test_status_group_runtime_issue_when_remote_health_failed(self, tmp_path, monkeypatch):
        cmd_svc, query_svc, _, _, _ = _make_services()
        descriptor_path = self._write_market_descriptor(
            tmp_path / "remote-extension",
            extension_id="remote.extension",
            vendor="Acme",
            category="ops",
            tags=["remote"],
            execution_modes=["remote_https"],
            with_remote=True,
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=_ADMIN_SCOPE,
        )
        monkeypatch.setattr(
            "core.application.extensions.lifecycle_descriptor_io._resolve_hostname_ips",
            lambda _host, *, port: {"93.184.216.34"},
        )
        attempted = _stub_unreachable_remote_runtime(monkeypatch)
        cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=imported.release_id,
                runtime_mode="remote_https",
                remote_base_url="https://runtime.example.com",
            ),
            scope=_ADMIN_SCOPE,
        )

        rows = query_svc.list_extensions(scope=_ADMIN_SCOPE)
        assert rows.total == 1
        assert rows.extensions[0].handshake_status == "failed"
        assert rows.extensions[0].health_status == "failed"
        assert rows.extensions[0].status_group == "runtime_issue"
        assert attempted == [
            "https://runtime.example.com/handshake",
            "https://runtime.example.com/health",
        ]

    def test_marketplace_filters_facets_and_installed_overlay(self, tmp_path):
        cmd_svc, query_svc, _, _, _ = _make_services()
        desc_one = self._write_market_descriptor(
            tmp_path / "sales-extension",
            extension_id="vendor.sales",
            vendor="Acme",
            category="automation",
            tags=["sales", "crm"],
            execution_modes=["local"],
            billing_mode="subscription",
            plan_id="pro",
        )
        desc_two = self._write_market_descriptor(
            tmp_path / "support-extension",
            extension_id="vendor.support",
            vendor="Contoso",
            category="support",
            tags=["support"],
            execution_modes=["local"],
            allows_self_hosted=False,
            requires_connected_control_plane=True,
            billing_mode="usage",
            plan_id="team",
            include_agent=False,
        )
        release_one = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(desc_one)),
            scope=_ADMIN_SCOPE,
        )
        cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(desc_two)),
            scope=_ADMIN_SCOPE,
        )
        installed = cmd_svc.install_release(
            ExtensionReleaseInstallInput(
                release_id=release_one.release_id,
                runtime_mode="local",
                entitlement_status="active",
            ),
            scope=_ADMIN_SCOPE,
        )

        marketplace = query_svc.list_marketplace(
            filters={
                "category": ["automation"],
                "tags": ["sales"],
                "vendor": ["acme"],
                "contains": ["workflow", "agent"],
                "execution_mode": ["local"],
                "allows_self_hosted": True,
                "requires_connected_control_plane": False,
                "compatibility": ["compatible"],
                "billing_mode": ["subscription"],
                "plan_id": ["pro"],
                "facets": True,
            },
            scope=_ADMIN_SCOPE,
        )
        assert marketplace.total == 1
        assert len(marketplace.extensions) == 1
        row = marketplace.extensions[0]
        assert row.extension_id == "vendor.sales"
        assert row.installed is True
        assert row.install_id == installed.install_id
        assert row.status_group in {"usable", "unknown"}
        assert marketplace.facets is not None
        assert any(
            bucket.value == "automation" and bucket.count == 1
            for bucket in marketplace.facets.category
        )
        assert any(
            bucket.value == "compatible" and bucket.count == 1
            for bucket in marketplace.facets.compatibility
        )
        assert any(
            bucket.value == "subscription" and bucket.count == 1
            for bucket in marketplace.facets.billing_mode
        )
        assert any(
            bucket.value == "pro" and bucket.count == 1 for bucket in marketplace.facets.plan_id
        )

    def test_marketplace_hides_private_releases_from_other_accounts(self, tmp_path):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        cmd_svc, query_svc, _, _, _ = _make_services(store=store, runtime_store=runtime_store)
        descriptor_path = self._write_market_descriptor(
            tmp_path / "alpha-private-extension",
            extension_id="alpha.private.extension",
            vendor="Alpha",
            category="internal",
            tags=["private"],
            execution_modes=["local"],
            channel="private_byo",
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=ScopeContext(actor_id="admin", principal_type="admin", account_id="acc_alpha"),
        )
        assert imported.release_id

        alpha_marketplace = query_svc.list_marketplace(scope=ScopeContext(account_id="acc_alpha"))
        beta_marketplace = query_svc.list_marketplace(scope=ScopeContext(account_id="acc_beta"))
        assert any(
            row.extension_id == "alpha.private.extension" for row in alpha_marketplace.extensions
        )
        assert all(
            row.extension_id != "alpha.private.extension" for row in beta_marketplace.extensions
        )

    def test_marketplace_hides_tenant_owned_marketplace_releases_from_other_accounts(
        self, tmp_path
    ):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        cmd_svc, query_svc, _, _, _ = _make_services(store=store, runtime_store=runtime_store)
        descriptor_path = self._write_market_descriptor(
            tmp_path / "alpha-claimed-marketplace-extension",
            extension_id="alpha.claimed.marketplace",
            vendor="Alpha",
            category="internal",
            tags=["marketplace"],
            execution_modes=["local"],
            channel="marketplace_managed",
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=ScopeContext(actor_id="admin", principal_type="admin", account_id="acc_alpha"),
        )
        assert imported.release_id

        alpha_marketplace = query_svc.list_marketplace(scope=ScopeContext(account_id="acc_alpha"))
        beta_marketplace = query_svc.list_marketplace(scope=ScopeContext(account_id="acc_beta"))
        assert any(
            row.extension_id == "alpha.claimed.marketplace" for row in alpha_marketplace.extensions
        )
        assert all(
            row.extension_id != "alpha.claimed.marketplace" for row in beta_marketplace.extensions
        )

        _raises_input_5049_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id, runtime_mode="local"
        )
        _raises_input_5049_2 = ScopeContext(principal_type="admin", account_id="acc_beta")
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            cmd_svc.install_release(_raises_input_5049_1, scope=_raises_input_5049_2)

    def test_marketplace_keeps_default_account_marketplace_releases_public(self, tmp_path):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        cmd_svc, query_svc, _, _, _ = _make_services(store=store, runtime_store=runtime_store)
        descriptor_path = self._write_market_descriptor(
            tmp_path / "platform-marketplace-extension",
            extension_id="platform.marketplace",
            vendor="Platform",
            category="automation",
            tags=["marketplace"],
            execution_modes=["local"],
            channel="marketplace_managed",
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=ScopeContext(
                actor_id="admin", principal_type="admin", account_id=DEFAULT_ACCOUNT_ID
            ),
        )

        beta_marketplace = query_svc.list_marketplace(scope=ScopeContext(account_id="acc_beta"))
        assert any(
            row.extension_id == "platform.marketplace" for row in beta_marketplace.extensions
        )
        _raises_input_5082_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id, runtime_mode="local"
        )
        _raises_input_5082_2 = ScopeContext(principal_type="admin", account_id="acc_beta")
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            cmd_svc.install_release(_raises_input_5082_1, scope=_raises_input_5082_2)

    def test_install_release_rejects_cross_account_private_release_id(self, tmp_path):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        cmd_svc, _, _, _, _ = _make_services(store=store, runtime_store=runtime_store)
        descriptor_path = self._write_market_descriptor(
            tmp_path / "alpha-private-install-extension",
            extension_id="alpha.install.private",
            vendor="Alpha",
            category="internal",
            tags=["private"],
            execution_modes=["local"],
            channel="private_byo",
        )
        imported = cmd_svc.import_descriptor_file(
            ExtensionDescriptorImportFileInput(descriptor_path=str(descriptor_path)),
            scope=ScopeContext(actor_id="admin", principal_type="admin", account_id="acc_alpha"),
        )

        _raises_input_5106_1 = ExtensionReleaseInstallInput(
            release_id=imported.release_id, runtime_mode="local"
        )
        _raises_input_5106_2 = ScopeContext(principal_type="admin", account_id="acc_beta")
        with pytest.raises(ServiceValidationError, match="deployment-scoped"):
            cmd_svc.install_release(_raises_input_5106_1, scope=_raises_input_5106_2)

    def test_list_extensions_fails_closed_when_runtime_store_lookup_raises(self):
        class BrokenRuntimeStore:
            def list_entitlements(self, *args, **kwargs):
                raise RuntimeError("entitlements unavailable")

            def list_releases(self, *args, **kwargs):
                raise RuntimeError("releases unavailable")

            def list_artifact_snapshots_for_releases(self, *args, **kwargs):
                raise RuntimeError("artifacts unavailable")

        _cmd_svc, query_svc, store, _, _ = _make_services(runtime_store=BrokenRuntimeStore())
        store.install(
            extension_id="resilient.extension",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )

        with pytest.raises(ServiceUnavailableError, match="entitlement store"):
            query_svc.list_extensions(scope=_ADMIN_SCOPE)

    def test_marketplace_propagates_store_outages_as_unavailable(self):
        class BrokenRuntimeStore:
            def list_releases(self, *args, **kwargs):
                raise RuntimeError("release store unavailable")

            def list_artifact_snapshots_for_releases(self, *args, **kwargs):
                raise RuntimeError("artifact store unavailable")

            def list_entitlements(self, *args, **kwargs):
                raise RuntimeError("entitlement store unavailable")

        class BrokenInstallStore:
            def list_for_account(self, *args, **kwargs):
                raise RuntimeError("install store unavailable")

        query_svc = ExtensionQueryService(
            get_logger=lambda: MagicMock(),
            get_install_store=lambda: BrokenInstallStore(),
            get_runtime_store=lambda: BrokenRuntimeStore(),
        )

        with pytest.raises(ServiceUnavailableError, match="release store"):
            query_svc.list_marketplace(filters={"facets": True}, scope=_ADMIN_SCOPE)

        client, _, _, _ = _make_complete_extension_api_client(query_service=query_svc)
        response = client.get("/api/extensions/marketplace")
        assert response.status_code == 503

    @pytest.mark.parametrize(
        ("failed_method", "message"),
        (
            ("list_for_account", "install store"),
            ("list_entitlements", "entitlement store"),
            ("list_releases", "release store"),
            ("list_artifact_snapshots_for_releases", "artifact store"),
        ),
    )
    def test_installed_list_propagates_store_outages_as_unavailable(self, failed_method, message):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        store.install(
            extension_id="acme.store-outage",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            release_id="rel-store-outage",
        )
        target = store if failed_method == "list_for_account" else runtime_store
        setattr(target, failed_method, MagicMock(side_effect=RuntimeError("offline")))
        query_svc = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        )

        with pytest.raises(ServiceUnavailableError, match=message):
            query_svc.list_extensions(scope=_ADMIN_SCOPE)

    def test_default_store_acquisition_failure_does_not_fall_back_to_memory(self):
        query_svc = ExtensionQueryService(
            get_logger=MagicMock(side_effect=RuntimeError("database unavailable")),
        )

        with pytest.raises(ServiceUnavailableError, match="install store"):
            query_svc.list_extensions(scope=_ADMIN_SCOPE)

    def test_default_runtime_store_acquisition_failure_does_not_fall_back_to_memory(self):
        store = _make_install_store()
        store.install(
            extension_id="acme.runtime-store-outage",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
        )
        query_svc = ExtensionQueryService(
            get_logger=MagicMock(side_effect=RuntimeError("database unavailable")),
            get_install_store=lambda: store,
        )

        with pytest.raises(ServiceUnavailableError, match="runtime store"):
            query_svc.list_extensions(scope=_ADMIN_SCOPE)

    @pytest.mark.parametrize(
        ("failed_method", "message"),
        (
            ("get", "install store"),
            ("get_release", "release store"),
            ("list_entitlements", "entitlement store"),
            ("list_artifact_snapshots", "artifact store"),
        ),
    )
    def test_extension_detail_propagates_store_outages_as_unavailable(self, failed_method, message):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": "rel-detail-outage",
                "extension_id": "acme.detail-outage",
                "descriptor": {"extension_id": "acme.detail-outage"},
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
        )
        target = store if failed_method == "get" else runtime_store
        setattr(target, failed_method, MagicMock(side_effect=RuntimeError("offline")))
        query_svc = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        )
        client, _, _, _ = _make_complete_extension_api_client(query_service=query_svc)

        response = client.get(f"/api/extensions/{release.extension_id}")

        assert response.status_code == 503
        assert message in response.text

    @pytest.mark.parametrize(
        ("runtime_status", "health_status", "expected"),
        (
            ("compatible", "ok", "healthy"),
            ("compatible", "degraded", "degraded"),
            ("compatible", "failed", "failed"),
            ("failed", "ok", "failed"),
            ("incompatible", "healthy", "failed"),
            ("unavailable", "ok", "failed"),
        ),
    )
    def test_passive_health_failure_signals_take_precedence(
        self, runtime_status, health_status, expected
    ):
        store = _make_install_store()
        runtime_store = _make_runtime_store()
        release = runtime_store.create_release(
            {
                "release_id": f"rel-health-{runtime_status}-{health_status}",
                "extension_id": "acme.health-matrix",
                "descriptor": {
                    "extension_id": "acme.health-matrix",
                    "runtime": {"contract_version": "extension_host_v2"},
                },
            }
        )
        store.install(
            extension_id=release.extension_id,
            version=release.release_version,
            account_id=DEFAULT_ACCOUNT_ID,
            release_id=release.release_id,
            runtime_status=runtime_status,
            health_status=health_status,
        )
        query_svc = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: store,
            get_runtime_store=lambda: runtime_store,
        )

        result = query_svc.get_health(release.extension_id, scope=_ADMIN_SCOPE)

        assert result.health["status"] == expected

    def test_connection_count_lookup_failures_do_not_break_extension_list_endpoints(self):
        class BrokenCursorContext:
            def __enter__(self):
                raise RuntimeError("project_connections query failed")

            def __exit__(self, exc_type, exc, tb):
                return False

        class BrokenLogger:
            def cursor(self):
                return BrokenCursorContext()

        _cmd_svc, query_svc, store, _, _ = _make_services(bl=BrokenLogger())
        store.install(
            extension_id="conn.fail.soft",
            version="1.0.0",
            account_id=DEFAULT_ACCOUNT_ID,
            installed_by="test",
        )

        installed = query_svc.list_extensions(scope=_ADMIN_SCOPE)
        assert installed.total == 1
        assert installed.extensions[0].extension_id == "conn.fail.soft"

        marketplace = query_svc.list_marketplace(filters={"facets": True}, scope=_ADMIN_SCOPE)
        assert marketplace.total >= 0
        assert marketplace.facets is not None
