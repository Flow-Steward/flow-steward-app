"""Integration coverage for the extension remote runtime lifecycle story."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.query_service import ExtensionQueryService
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.extensions.service_support import (
    _runtime_platform_version,
    _sign_runtime_token,
)
from core.application.management.command_services import ExtensionBindingService
from core.application.management.scope import ScopeContext
from core.entrypoints.api.integrations_connectors import create_integrations_connectors_router
from core.infrastructure.extension_runtime.extension_runtime import (
    emit_extension_event,
    run_extension_action,
    run_extension_query,
)

pytestmark = [pytest.mark.integration]
TEST_ACCOUNT_ID = DEFAULT_ACCOUNT_ID

SCOPE_FIELDS = ["group_id", "for_each_id", "step_id", "step_execution_id"]


def _decode_b64url_json(segment: str) -> dict[str, Any]:
    padded = f"{segment}{'=' * (-len(segment) % 4)}"
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    return dict(data) if isinstance(data, dict) else {}


def _service_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    return JSONResponse(
        {"error": getattr(exc, "message", str(exc))},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _api_error_response(exc, *_args, **_kwargs) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=500)


def _make_extension_api_client() -> tuple[TestClient, ExtensionInstallStore, ExtensionRuntimeStore]:
    store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    bl = MagicMock()
    bl.get_extension.return_value = None
    cursor = bl.conn.cursor.return_value.__enter__.return_value

    def fetchone():
        call = cursor.execute.call_args
        statement = str(call.args[0]) if call and call.args else ""
        return (
            (True,)
            if "pg_try_advisory_lock" in statement or "pg_advisory_unlock" in statement
            else None
        )

    cursor.fetchone.side_effect = fetchone
    # Issuing a runtime token scoped to a project resolves the project through
    # the logger and enforces account ownership; provide a project owned by the
    # test account so the project-access guard passes.
    bl.get_project.return_value = {"id": 7, "account_id": TEST_ACCOUNT_ID, "name": "Project 7"}
    app = FastAPI()
    lifecycle_service = ExtensionBindingService(
        get_logger=lambda: bl,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
        get_extension_install_store=lambda: store,
        get_extension_runtime_store=lambda: runtime_store,
        get_operation_store=lambda: bl,
        extension_query_service_factory=lambda *, bl, install_store, runtime_store: (
            ExtensionQueryService(
                get_logger=lambda: bl,
                get_install_store=lambda: install_store,
                get_runtime_store=lambda: runtime_store,
            )
        ),
    )

    def _request_scope(request: Request, **kwargs):
        return ScopeContext(
            account_id=str(request.headers.get("x-test-account-id") or "acc_default").strip(),
            actor_id=str(request.headers.get("x-test-actor") or "api").strip(),
            principal_type=str(request.headers.get("x-test-role") or "admin").strip().lower(),
            project_id=kwargs.get("project_id"),
        )

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: bl,
            get_extension_install_store=lambda: store,
            get_extension_runtime_store=lambda: runtime_store,
            extension_query_service=lambda: ExtensionQueryService(
                get_logger=lambda: bl,
                get_install_store=lambda: store,
                get_runtime_store=lambda: runtime_store,
            ),
            extension_binding_service=lambda: lifecycle_service,
            request_scope=_request_scope,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            service_error_response=_service_error_response,
            api_error_response=_api_error_response,
            err_not_found="Not found",
        )
    )
    return TestClient(app, raise_server_exceptions=False), store, runtime_store


def _write_remote_descriptor(root: Path) -> Path:
    (root / "artifacts" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "workflows" / "wf_sync.yaml").write_text(
        "workflow_id: wf_sync\n", encoding="utf-8"
    )
    (root / "artifacts" / "agents" / "agent_sync.yaml").write_text(
        "agent_id: agent_sync\n", encoding="utf-8"
    )
    descriptor = {
        "schema_version": "extension_descriptor_v1",
        "extension_id": "acme.remote-extension",
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {
            "display_name": "Remote Extension",
            "description": "Strict remote runtime story",
            "tags": ["remote", "runtime"],
        },
        "vendor": {"vendor_name": "Acme", "support_url": "https://acme.example.com/support"},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["remote_https"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": _runtime_platform_version()},
            "remote": {
                "base_url": "https://runtime.example.com/base",
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/queries/{query_id}",
                    "action": "/actions/{action_id}",
                    "emit_event": "/events",
                },
            },
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
    descriptor_path = root / "extension-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    return descriptor_path


def _connection_rows(*, install_id: str) -> list[dict[str, Any]]:
    return [
        {
            "connection_id": "conn_rt_story",
            "project_id": 7,
            "account_id": TEST_ACCOUNT_ID,
            "connection_kind": "extension_runtime",
            "extension_install_id": install_id,
            "runtime_mode": "remote_https",
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


def _remote_extension_payload(
    *,
    base_url: str,
    install_id: str,
    extension_id: str,
    token: str,
) -> dict[str, Any]:
    return {
        "extension_key": "acme.remote-extension.runtime",
        "install_id": install_id,
        "extension_install_id": install_id,
        "extension_id": extension_id,
        "connection_id": "conn_rt_story",
        "runtime_mode": "remote_https",
        "remote_base_url": base_url,
        "remote_bearer_token": token,
        "runtime_status": "compatible",
        "handshake_status": "ok",
        "health_status": "ok",
        "runtime": {
            "contract_version": "extension_host_v2",
            "remote": {
                "base_url": base_url,
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                    "query": "/queries/{query_id}",
                    "action": "/actions/{action_id}",
                    "emit_event": "/events",
                },
            },
        },
        "query_definitions": [{"query_id": "list_accounts", "handler": {"mode": "extension"}}],
        "action_definitions": [{"action_id": "sync_account", "handler": {"mode": "extension"}}],
        "supported_events": ["external.comment.received"],
    }


def _issue_runtime_token(
    client: TestClient,
    *,
    install_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/extensions/runtime/token",
        headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
        json={
            "install_id": install_id,
            "connection_id": "conn_rt_story",
            "project_id": 7,
            "ttl_seconds": 300,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["issued"] is True
    assert body["token_type"] == "Bearer"
    assert body["install_id"] == install_id
    return body


def _import_and_install_remote_extension(
    client: TestClient,
    *,
    descriptor_path: Path,
    remote_base_url: str,
) -> tuple[str, str]:
    imported = client.post(
        "/api/extensions/descriptors/import/file",
        headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
        json={"descriptor_path": str(descriptor_path)},
    )
    assert imported.status_code == 200
    imported_body = imported.json()
    assert imported_body["status"] == "imported"
    release_id = imported_body["release_id"]

    with patch(
        "core.application.extensions.command_install._safe_remote_http_url",
        side_effect=lambda url, *, purpose: str(url),
    ):
        installed = client.post(
            f"/api/extensions/releases/{release_id}/install",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
            json={
                "release_id": release_id,
                "runtime_mode": "remote_https",
                "remote_base_url": remote_base_url,
                "entitlement_status": "active",
            },
        )
    assert installed.status_code == 200
    installed_body = installed.json()
    assert installed_body["status"] == "installed"
    assert installed_body["extension_id"] == "acme.remote-extension"
    return release_id, installed_body["install_id"]


class _AuthRuntimeHandler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[tuple[str, str], tuple[int, dict[str, Any]]]] = {}
    requests: ClassVar[list[dict[str, Any]]] = []
    token_secret: str = ""
    protected_paths: ClassVar[set[str]] = {
        "/queries/list_accounts",
        "/actions/sync_account",
        "/events",
    }

    def _handle(self) -> None:
        path = str(self.path or "").split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        payload: dict[str, Any] = {}
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
                payload = decoded if isinstance(decoded, dict) else {"raw": decoded}
            except Exception:
                payload = {"raw": raw.decode("utf-8", errors="replace")}

        auth_header = str(self.headers.get("Authorization") or "")
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": path,
                "authorization": auth_header,
                "payload": payload,
            }
        )

        if path in self.__class__.protected_paths:
            valid, detail = self._validate_bearer(auth_header)
            if not valid:
                self._respond(
                    401,
                    {
                        "ok": False,
                        "error": {"code": "authentication_error", "message": detail},
                    },
                )
                return

        status, response = self.__class__.routes.get(
            (self.command, path),
            (404, {"ok": False, "error": {"message": "not found"}}),
        )
        self._respond(status, response)

    def _validate_bearer(self, header_value: str) -> tuple[bool, str]:
        prefix = "Bearer "
        if not header_value.startswith(prefix):
            return False, "missing bearer token"
        token = header_value[len(prefix) :].strip()
        parts = token.split(".")
        if len(parts) != 3:
            return False, "invalid token"
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self.__class__.token_secret.encode("utf-8"),
                    signing_input,
                    hashlib.sha256,
                ).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )
        if not hmac.compare_digest(expected_sig, parts[2]):
            return False, "invalid token signature"
        payload = _decode_b64url_json(parts[1])
        if str(payload.get("aud") or "") != "extension-runtime":
            return False, "invalid token audience"
        exp_value = int(payload.get("exp") or 0)
        if exp_value and exp_value <= int(time.time()):
            return False, "token expired"
        if not str(payload.get("sub") or "").strip():
            return False, "missing token subject"
        return True, ""

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload or {}).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format, *args):
        _ = (format, args)


@pytest.fixture
def remote_runtime_server(monkeypatch):
    monkeypatch.setenv("FS_EXTENSION_RUNTIME_SIGNING_SECRET", "test-runtime-secret")
    # The stub runtime binds to loopback; opt into private/loopback remote URLs
    # so the SSRF preflight allows dialing 127.0.0.1 in this integration test.
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    _AuthRuntimeHandler.token_secret = "test-runtime-secret"
    _AuthRuntimeHandler.routes = {
        ("GET", "/handshake"): (
            200,
            {
                "ok": True,
                "result": {
                    "status": "ok",
                    "contract_version": "extension_host_v2",
                    "execution_scope_fields": SCOPE_FIELDS,
                },
            },
        ),
        ("POST", "/handshake"): (
            200,
            {
                "ok": True,
                "result": {
                    "status": "ok",
                    "contract_version": "extension_host_v2",
                    "execution_scope_fields": SCOPE_FIELDS,
                },
            },
        ),
        ("GET", "/health"): (200, {"ok": True, "result": {"status": "healthy"}}),
        ("POST", "/queries/list_accounts"): (
            200,
            {"ok": True, "result": {"items": [{"id": "acc_1"}], "next_cursor": "", "total": 1}},
        ),
        ("POST", "/actions/sync_account"): (200, {"ok": True, "result": {"status": "done"}}),
        ("POST", "/events"): (200, {"ok": True, "result": {"accepted": True}}),
    }
    _AuthRuntimeHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthRuntimeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http_base_url = f"http://127.0.0.1:{server.server_port}"
    base_url = f"https://runtime.test:{server.server_port}"
    from tests._remote_https_test_transport import install_loopback_https_runtime_transport

    install_loopback_https_runtime_transport(
        monkeypatch,
        https_base_url=base_url,
        http_base_url=http_base_url,
    )
    try:
        yield {"base_url": base_url, "handler": _AuthRuntimeHandler}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class TestExtensionRemoteRuntimeStory:
    def test_remote_runtime_story_persists_state_and_authenticates_operations(
        self, tmp_path, remote_runtime_server
    ):
        client, _store, runtime_store = _make_extension_api_client()
        descriptor_path = _write_remote_descriptor(tmp_path / "remote-extension")
        _release_id, install_id = _import_and_install_remote_extension(
            client,
            descriptor_path=descriptor_path,
            remote_base_url=remote_runtime_server["base_url"],
        )

        detail = client.get(
            f"/api/extensions/installs/{install_id}",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
        )
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["extension_id"] == "acme.remote-extension"
        assert detail_body["state"] == "active"
        assert detail_body["runtime_mode"] == "remote_https"
        assert detail_body["remote_base_url"] == remote_runtime_server["base_url"]
        assert detail_body["handshake_status"] == "ok"
        assert detail_body["health_status"] == "ok"
        assert detail_body["runtime_status"] == "compatible"
        assert len(detail_body["projections"]) == 2
        projections = runtime_store.list_projections(account_id=TEST_ACCOUNT_ID, only_active=False)
        assert len(projections) == 2
        assert all(row.is_active for row in projections)

        with patch(
            "core.application.extensions.command_install._safe_remote_http_url",
            side_effect=lambda url, *, purpose: str(url),
        ):
            handshake = client.post(
                f"/api/extensions/installs/{install_id}/handshake",
                headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
            )
        assert handshake.status_code == 200
        handshake_body = handshake.json()
        assert handshake_body["status"] == "ok"
        assert handshake_body["handshake_status"] == "ok"
        assert handshake_body["health_status"] == "ok"
        assert handshake_body["runtime_status"] == "compatible"

        with patch(
            "core.application.extensions.command_install._list_extension_runtime_connections",
            return_value=_connection_rows(install_id=install_id),
        ):
            token_body = _issue_runtime_token(client, install_id=install_id)

        payload = _decode_b64url_json(token_body["token"].split(".")[1])
        assert payload["iss"] == "flow-steward-extension-runtime"
        assert payload["sub"] == install_id
        assert payload["aud"] == "extension-runtime"
        assert payload["account_id"] == TEST_ACCOUNT_ID
        assert payload["extension_id"] == "acme.remote-extension"
        assert payload["connection_id"] == "conn_rt_story"
        assert payload["project_id"] == "7"

        extension = _remote_extension_payload(
            base_url=remote_runtime_server["base_url"],
            install_id=install_id,
            extension_id="acme.remote-extension",
            token=token_body["token"],
        )
        query = run_extension_query(
            extension,
            query_id="list_accounts",
            context={"project_id": 7},
            params={"limit": 10},
            runtime_context={
                "job_id": 42,
                "group_id": "accounts",
                "step_id": "fetch",
                "step_execution_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            },
        )
        action = run_extension_action(
            extension,
            action_id="sync_account",
            context={"project_id": 7},
            input_payload={"account_id": "acc_1"},
            runtime_context={
                "job_id": 42,
                "group_id": "accounts",
                "step_id": "sync",
                "step_execution_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            },
        )
        event = emit_extension_event(
            extension,
            event_type="external.comment.received",
            payload={"message_id": "msg_1"},
            project_id=7,
            job_id=42,
            group_id="accounts",
            step_id="notify",
            step_execution_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            runtime_context={"account_id": TEST_ACCOUNT_ID},
        )

        assert query["status"] == "succeeded"
        assert query["handler_mode"] == "remote_https"
        assert action["status"] == "succeeded"
        assert action["handler_mode"] == "remote_https"
        assert event["status"] == "accepted"
        assert event["handler_mode"] == "remote_https"

        requests = remote_runtime_server["handler"].requests
        paths = [row["path"] for row in requests]
        assert paths.count("/handshake") >= 2
        assert paths.count("/health") >= 2
        assert "/queries/list_accounts" in paths
        assert "/actions/sync_account" in paths
        assert "/events" in paths

        protected = [
            row
            for row in requests
            if row["path"] in {"/queries/list_accounts", "/actions/sync_account", "/events"}
        ]
        assert len(protected) == 3
        assert all(row["authorization"] == f"Bearer {token_body['token']}" for row in protected)
        assert protected[0]["payload"]["connection"]["install_id"] == install_id
        assert protected[0]["payload"]["connection"]["connection_id"] == "conn_rt_story"
        assert protected[0]["payload"]["scope"]["project_id"] == "7"
        assert protected[1]["payload"]["scope"]["step_id"] == "sync"
        assert protected[2]["payload"]["scope"]["job_id"] == 42
        assert protected[2]["payload"]["scope"]["step_id"] == "notify"
        for row, execution_id in zip(
            protected,
            (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            ),
            strict=True,
        ):
            assert row["payload"]["contract_version"] == "extension_host_v2"
            assert row["payload"]["scope"]["group_id"] == "accounts"
            assert row["payload"]["scope"]["step_execution_id"] == execution_id
            assert "phase_id" not in row["payload"]["scope"]

    def test_remote_runtime_story_rejects_expired_token_and_denies_after_disable_uninstall(
        self, tmp_path, remote_runtime_server
    ):
        client, _store, runtime_store = _make_extension_api_client()
        descriptor_path = _write_remote_descriptor(tmp_path / "remote-extension-denials")
        _release_id, install_id = _import_and_install_remote_extension(
            client,
            descriptor_path=descriptor_path,
            remote_base_url=remote_runtime_server["base_url"],
        )

        now = int(time.time())
        expired_token = _sign_runtime_token(
            {
                "iss": "flow-steward-extension-runtime",
                "sub": install_id,
                "aud": "extension-runtime",
                "account_id": TEST_ACCOUNT_ID,
                "extension_id": "acme.remote-extension",
                "connection_id": "conn_rt_story",
                "project_id": 7,
                "iat": now - 600,
                "exp": now - 60,
            },
            secret="test-runtime-secret",
        )
        expired_extension = _remote_extension_payload(
            base_url=remote_runtime_server["base_url"],
            install_id=install_id,
            extension_id="acme.remote-extension",
            token=expired_token,
        )
        expired_query = run_extension_query(
            expired_extension,
            query_id="list_accounts",
            context={"project_id": 7},
            params={"limit": 1},
        )
        assert expired_query["status"] == "failed"
        expired_detail = str(expired_query.get("detail") or "").lower()
        assert expired_detail == "authentication_error"
        assert remote_runtime_server["handler"].requests[-1]["path"] == "/queries/list_accounts"
        assert (
            remote_runtime_server["handler"].requests[-1]["authorization"]
            == f"Bearer {expired_token}"
        )

        disabled = client.post(
            "/api/extensions/acme.remote-extension/disable",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["state"] == "disabled"
        projections_after_disable = runtime_store.list_projections(
            account_id=TEST_ACCOUNT_ID,
            only_active=False,
        )
        assert len(projections_after_disable) == 2
        assert all(row.is_active is False for row in projections_after_disable)

        denied_after_disable = client.post(
            "/api/extensions/runtime/token",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
            json={"install_id": install_id},
        )
        assert denied_after_disable.status_code == 200
        denied_disable_body = denied_after_disable.json()
        assert denied_disable_body["issued"] is False
        assert "not active" in denied_disable_body["reason"].lower()

        uninstall_plan = client.get(
            "/api/extensions/acme.remote-extension/uninstall-plan",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
        )
        assert uninstall_plan.status_code == 200, uninstall_plan.text
        uninstalled = client.post(
            "/api/extensions/acme.remote-extension/uninstall",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
            json={"revision": uninstall_plan.json()["revision"]},
        )
        assert uninstalled.status_code == 200
        assert uninstalled.json()["state"] == "uninstalled"
        assert runtime_store.list_projections(account_id=TEST_ACCOUNT_ID, only_active=False) == []

        denied_after_uninstall = client.post(
            "/api/extensions/runtime/token",
            headers={"x-test-account-id": TEST_ACCOUNT_ID, "x-test-actor": "checker"},
            json={"install_id": install_id},
        )
        assert denied_after_uninstall.status_code == 404
        assert "not found" in denied_after_uninstall.json()["error"].lower()
