from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.callbacks import (
    ExtensionCallbackRegistrationService,
    _base_url_from_request,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.extension_callbacks import create_extension_callbacks_router


def test_maintenance_classifies_extension_callback_ingress_before_dispatch() -> None:
    from core.infrastructure.runtime.maintenance_http import maintenance_request_decision

    for method in ("GET", "POST"):
        decision = maintenance_request_decision(
            method,
            "/webhook/extensions/fixture.callback-provider/updates/token",
            authenticated=False,
        )
        assert decision.allowed is False
        assert decision.code == "maintenance_mode_enabled"


class FakeCallbackLogger:
    def __init__(self) -> None:
        # In-memory callback behavior fixture: transaction SQL is not its storage.
        # Actual guards/version writes and rollback are covered on PostgreSQL.
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        self.registrations: dict[str, dict[str, Any]] = {}
        self.secrets: dict[tuple[str, str], str] = {}
        self.emitted: list[dict[str, Any]] = []
        self.callback_deliveries: set[tuple[str, str]] = set()
        self.project_account_id = DEFAULT_ACCOUNT_ID
        self.extension_account_id = DEFAULT_ACCOUNT_ID
        self.project_id = "00000000-0000-0000-0000-000000000003"
        self.connections: dict[str, dict[str, Any]] = {
            "conn-callback-primary": {
                "connection_id": "conn-callback-primary",
                "project_id": self.project_id,
                "account_id": DEFAULT_ACCOUNT_ID,
                "extension_id": "fixture.callback-provider",
                "extension_install_id": "install-fixture-callback-provider",
                "connection_config": {"destination_id": "-100123"},
                "secret_refs": ["api_token"],
                "enabled": True,
            }
        }
        self.extension = {
            "extension_key": "fixture.callback-provider",
            "extension_id": "fixture.callback-provider",
            "install_id": "install-fixture-callback-provider",
            "account_id": self.extension_account_id,
            "enabled": True,
            "compiled_manifest": {
                "extension_id": "fixture.callback-provider",
                "supported_events": [
                    "notification.interaction_received",
                    "external.comment.received",
                ],
                "callbacks": [
                    {
                        "callback_id": "updates",
                        "purpose": "inbound_events",
                        "methods": ["POST"],
                        "action_id": "ingress_event",
                        "connection_scoped": False,
                        "auth": {
                            "type": "shared_secret_header",
                            "header": "X-Fixture-Callback-Secret",
                        },
                    }
                ],
            },
            "action_definitions": [
                {"action_id": "ingress_event"},
                {"action_id": "configure_webhook"},
                {"action_id": "get_webhook_status"},
            ],
        }

    def lock_extension_callback_registration_scope(self, **_kwargs: Any) -> None:
        return None

    def get_extension(self, extension_id: str, *, account_id: str | None = None) -> dict | None:
        if extension_id != "fixture.callback-provider":
            return None
        if account_id and account_id != self.extension_account_id:
            return None
        extension = dict(self.extension)
        extension["account_id"] = self.extension_account_id
        return extension

    def get_extension_by_install_id(
        self, install_id: str, *, account_id: str | None = None
    ) -> dict | None:
        if install_id != "install-fixture-callback-provider":
            return None
        return self.get_extension("fixture.callback-provider", account_id=account_id)

    def get_project(self, project_id: str) -> dict:
        return {"id": str(project_id), "account_id": self.project_account_id}

    def create_extension_callback_registration(self, **kwargs: Any) -> dict:
        row = {
            "id": kwargs["registration_id"],
            "extension_id": kwargs["extension_id"],
            "callback_id": kwargs["callback_id"],
            "callback_token_hash": kwargs["callback_token_hash"],
            "project_id": kwargs["project_id"],
            "connection_id": kwargs.get("connection_id"),
            "enabled": kwargs.get("enabled", True),
            "auth_type": kwargs.get("auth_type", "none"),
            "auth_config": dict(kwargs.get("auth_config") or {}),
        }
        self.registrations[row["id"]] = row
        return row

    def list_extension_callback_registrations(self, **kwargs: Any) -> list[dict]:
        extension_id = str(kwargs.get("extension_id") or "")
        callback_id = str(kwargs.get("callback_id") or "")
        project_id = str(kwargs.get("project_id") or "")
        return [
            row
            for row in self.registrations.values()
            if str(row.get("extension_id") or "") == extension_id
            and (not callback_id or str(row.get("callback_id") or "") == callback_id)
            and (not project_id or str(row.get("project_id") or "") == project_id)
        ]

    def get_extension_callback_registration_by_token_hash(
        self,
        *,
        callback_token_hash: str,
    ) -> dict | None:
        for row in self.registrations.values():
            if row.get("callback_token_hash") == callback_token_hash:
                return dict(row)
        return None

    def get_extension_callback_registration_by_id(self, registration_id: str) -> dict | None:
        return self.registrations.get(registration_id)

    def update_extension_callback_registration_token(self, **kwargs: Any) -> dict | None:
        row = self.registrations.get(kwargs["registration_id"])
        if not row:
            return None
        row["callback_token_hash"] = kwargs["callback_token_hash"]
        return dict(row)

    def mark_extension_callback_registration_seen(self, **kwargs: Any) -> None:
        row = self.registrations.get(kwargs["registration_id"])
        if row is not None:
            row["last_error"] = kwargs.get("error", "")

    def claim_extension_callback_delivery(self, **kwargs: Any) -> bool:
        key = (str(kwargs["registration_id"]), str(kwargs["delivery_id"]))
        if key in self.callback_deliveries:
            return False
        self.callback_deliveries.add(key)
        return True

    def save_project_secret(self, **kwargs: Any) -> bool:
        self.secrets[(str(kwargs["project_id"]), str(kwargs["secret_key"]))] = str(
            kwargs["secret_value"]
        )
        return True

    def get_project_secret(self, **kwargs: Any) -> str:
        return self.secrets.get((str(kwargs["project_id"]), str(kwargs["secret_key"])), "")

    def get_project_connection(self, **kwargs: Any) -> dict:
        if str(kwargs.get("extension_id") or "") != "fixture.callback-provider":
            return {}
        connection_id = str(kwargs.get("connection_id") or "")
        if connection_id:
            return dict(self.connections.get(connection_id) or {})
        project_id = str(kwargs.get("project_id") or "")
        for row in self.connections.values():
            if project_id and str(row.get("project_id") or "") != project_id:
                continue
            return dict(row)
        return {}

    def get_provider_secret(self, **kwargs: Any) -> str:
        if (
            str(kwargs.get("provider_key") or "") == "fixture.callback-provider"
            and str(kwargs.get("secret_key") or "") == "api_token"
        ):
            return "fixture-token"
        return ""

    def record_domain_event(self, **kwargs: Any) -> dict:
        self.emitted.append(kwargs)
        return {"event_id": kwargs.get("event_id")}

    def enqueue_domain_event(self, **kwargs: Any) -> None:
        self.emitted.append({"queued": kwargs})

    def cursor(self):
        return None


class _CallbackStoreWithoutProject:
    def __init__(self, delegate: FakeCallbackLogger) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        if name == "get_project":
            raise AttributeError(name)
        return getattr(self._delegate, name)


def _client(
    fake: FakeCallbackLogger,
    *,
    account_id: str = DEFAULT_ACCOUNT_ID,
    callback_store: Any | None = None,
    project_repository: Any | None = None,
    oauth_authorization_callback: Any | None = None,
    event_command_service_factory: Any | None = None,
    request_thread_ids: list[int] | None = None,
) -> TestClient:
    app = FastAPI()
    if request_thread_ids is not None:

        @app.middleware("http")
        async def capture_request_thread(request: Request, call_next):
            request_thread_ids.append(threading.get_ident())
            return await call_next(request)

    app.include_router(
        create_extension_callbacks_router(
            get_callback_store=lambda: callback_store or fake,
            get_project_repository=lambda: project_repository or fake,
            request_scope=lambda request, **kwargs: ScopeContext(
                account_id=account_id,
                project_id=kwargs.get("project_id"),
                actor_id=kwargs.get("actor_id"),
                principal_type="system",
            ),
            logger=type(
                "Logger",
                (),
                {
                    "exception": lambda *args, **kwargs: None,
                    "warning": lambda *args, **kwargs: None,
                },
            )(),
            err_database_not_configured="Database not configured",
            oauth_authorization_callback=oauth_authorization_callback,
            event_command_service_factory=event_command_service_factory,
        )
    )
    return TestClient(app)


def _assert_oauth_redirect(response: Any, expected_suffix: str) -> None:
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["location"] == (
        "/extensions/fixture.callback-provider?project_id="
        f"00000000-0000-0000-0000-000000000003&{expected_suffix}"
    )


def test_callback_routes_use_the_dedicated_project_repository_for_authorization():
    fake = FakeCallbackLogger()
    client = _client(
        fake,
        callback_store=_CallbackStoreWithoutProject(fake),
        project_repository=fake,
    )

    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )
    assert created.status_code == 200, created.text

    listed = client.get(
        "/api/extensions/fixture.callback-provider/callbacks"
        "?project_id=00000000-0000-0000-0000-000000000003"
    )
    assert listed.status_code == 200

    rotated = client.post(
        f"/api/extensions/fixture.callback-provider/callbacks/{created.json()['id']}/rotate",
        json={},
    )
    assert rotated.status_code == 200


def test_callback_webhook_openapi_uses_unique_operation_ids():
    fake = FakeCallbackLogger()
    client = _client(fake)

    schema = client.get("/openapi.json").json()
    path_item = schema["paths"]["/webhook/extensions/{extension_id}/{callback_id}/{callback_token}"]

    assert path_item["get"]["operationId"] == "get_extension_callback_webhook"
    assert path_item["post"]["operationId"] == "post_extension_callback_webhook"
    assert path_item["get"]["operationId"] != path_item["post"]["operationId"]


def test_oauth_callback_registration_is_stable_and_capability_is_secret_backed() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    service = ExtensionCallbackRegistrationService(fake)

    assert (
        service.oauth_registration_is_ready(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
        )
        is False
    )
    assert fake.registrations == {}

    first = service.ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    second = service.ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )

    assert first.callback_url == second.callback_url
    assert first.callback_token == second.callback_token
    assert len(fake.registrations) == 1
    registration = next(iter(fake.registrations.values()))
    token_ref = registration["auth_config"]["capability_token_ref"]
    assert fake.secrets[(fake.project_id, token_ref)] == first.callback_token
    assert first.callback_token not in repr(registration)
    assert (
        service.oauth_registration_is_ready(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
        )
        is True
    )


def test_oauth_callback_registration_never_follows_foreign_capability_ref() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    service = ExtensionCallbackRegistrationService(fake)
    created = service.ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    registration = fake.registrations[created.registration["id"]]
    registration["auth_config"]["capability_token_ref"] = "unrelated-project-secret"
    fake.secrets[(fake.project_id, "unrelated-project-secret")] = created.callback_token

    with pytest.raises(RuntimeError, match="capability is unavailable"):
        service.ensure_oauth_registration(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
            base_url="https://canonical.example.test",
        )


def test_oauth_callback_registration_does_not_silently_reenable_disabled_row() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    service = ExtensionCallbackRegistrationService(fake)
    created = service.ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    fake.registrations[created.registration["id"]]["enabled"] = False

    with pytest.raises(RuntimeError, match="registration is unavailable"):
        service.ensure_oauth_registration(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
            base_url="https://canonical.example.test",
        )
    assert fake.registrations[created.registration["id"]]["enabled"] is False


def test_generic_registration_cannot_create_or_rotate_reserved_oauth_callback() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    service = ExtensionCallbackRegistrationService(fake)
    with pytest.raises(ValueError, match="managed by core"):
        service.create_registration(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
            base_url="https://canonical.example.test",
        )


def test_reserved_oauth_callback_never_builds_or_dispatches_extension_envelope(monkeypatch) -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    invoked: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        lambda *_args, **kwargs: invoked.append(kwargs),
    )
    client = _client(fake)
    canary = "raw-provider-secret-canary"
    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?code={canary}&state={canary}&error_description={canary}",
        follow_redirects=False,
    )
    _assert_oauth_redirect(response, "oauth=error&reason=provider_unavailable")
    assert canary not in response.text
    assert invoked == []


def test_oauth_callback_passes_only_bounded_closed_query_values() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    received: list[dict[str, Any]] = []

    def callback(**values: Any):
        received.append(values)
        return {
            "outcome": "connected",
            "connection_id": "connection-safe",
        }

    client = _client(fake, oauth_authorization_callback=callback)
    canary = "raw-provider-copy-canary"
    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code"
        f"&error_description={canary}&error_uri=https://provider.test/{canary}",
        follow_redirects=False,
    )

    _assert_oauth_redirect(response, "oauth=success&oauth_connection_id=connection-safe")
    assert set(received[0]) == {
        "extension_id",
        "callback_id",
        "registration",
        "extension",
        "callback_path",
        "opaque_state",
        "code",
        "provider_error",
        "query_pairs",
    }
    assert received[0]["opaque_state"] == "opaque-state"
    assert received[0]["callback_path"].endswith(
        f"/oauth_authorization/{registration.callback_token}"
    )
    assert received[0]["code"] == "authorization-code"
    assert received[0]["provider_error"] is None
    # A provider that signs its callback signs everything it sent, so the raw
    # pairs travel as signature material. They are still quarantined: the closed
    # named fields never carry provider copy, and nothing renders the pairs.
    named = {key: value for key, value in received[0].items() if key != "query_pairs"}
    assert canary not in repr(named)
    assert canary not in response.text
    assert dict(received[0]["query_pairs"])["error_description"] == canary


def test_oauth_callback_filters_arbitrary_response_objects_from_handler() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    canary = "raw-callback-response-canary"
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: Response(canary, media_type="text/plain"),
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )

    _assert_oauth_redirect(response, "oauth=error&reason=restart_required")
    assert canary not in response.text


def test_oauth_callback_handler_exception_redirects_without_raw_provider_values() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    canary = "raw-provider-handler-canary"

    def fail(**_values: Any) -> None:
        raise RuntimeError(canary)

    response = _client(fake, oauth_authorization_callback=fail).get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state={canary}&code={canary}",
        follow_redirects=False,
    )

    _assert_oauth_redirect(response, "oauth=error&reason=restart_required")
    assert canary not in response.headers["location"]
    assert canary not in response.text


@pytest.mark.parametrize("outcome", ["needs_attention", "authorization_required"])
def test_oauth_callback_preserves_closed_retained_grant_outcomes(outcome: str) -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": outcome,
            "connection_id": "connection-1",
            "connection_preserved": True,
            "raw": "must-not-return-canary",
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )

    expected_reason = "provider_configuration_error" if outcome == "needs_attention" else outcome
    _assert_oauth_redirect(
        response,
        f"oauth=error&reason={expected_reason}&oauth_connection_id=connection-1"
        "&oauth_connection_preserved=1",
    )
    assert "canary" not in response.text


def test_oauth_callback_preserved_reconnect_prefers_return_page() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": "authorization_required",
            "connection_id": "connection-1",
            "connection_preserved": True,
            "return_page_id": "connections",
            "help_target": {
                "page_id": "oauth_help",
                "anchor": "authorization-required",
            },
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )

    _assert_oauth_redirect(
        response,
        "oauth=error&reason=authorization_required&extension_page=connections"
        "&oauth_connection_id=connection-1&oauth_connection_preserved=1",
    )
    assert response.headers["location"].endswith("oauth_connection_preserved=1")


def test_oauth_callback_omits_error_connection_id_without_explicit_preserved_flag() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": "authorization_required",
            "connection_id": "stale-connection",
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )

    _assert_oauth_redirect(response, "oauth=error&reason=authorization_required")
    assert "stale-connection" not in response.headers["location"]
    assert "oauth_connection_preserved" not in response.headers["location"]


@pytest.mark.parametrize(
    ("help_target", "expected_suffix"),
    [
        (
            {"page_id": "../unsafe", "anchor": "safe-anchor"},
            "oauth=error&reason=authorization_cancelled",
        ),
        (
            {"page_id": "oauth_help", "anchor": "Unsafe Anchor"},
            "oauth=error&reason=authorization_cancelled&extension_page=oauth_help",
        ),
        (
            {"page_id": "oauth_help", "anchor": "authorization-denied"},
            (
                "oauth=error&reason=authorization_cancelled&extension_page=oauth_help"
                "#authorization-denied"
            ),
        ),
    ],
)
def test_oauth_callback_filters_help_target_with_manifest_slug_grammar(
    help_target: dict[str, str], expected_suffix: str
) -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": "authorization_denied",
            "help_target": help_target,
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&error=access_denied",
        follow_redirects=False,
    )

    _assert_oauth_redirect(response, expected_suffix)


def test_oauth_callback_uses_return_page_and_scoped_connection_id() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": "connected",
            "connection_id": "conn-callback-primary",
            "return_page_id": "connections",
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )

    _assert_oauth_redirect(
        response,
        "oauth=success&extension_page=connections&oauth_connection_id=conn-callback-primary",
    )


def test_oauth_callback_help_target_precedes_return_page_and_filters_raw_ids() -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    client = _client(
        fake,
        oauth_authorization_callback=lambda **_values: {
            "outcome": "authorization_denied",
            "connection_id": "<unsafe>",
            "return_page_id": "connections",
            "help_target": {"page_id": "oauth_help", "anchor": "authorization-denied"},
        },
    )

    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?state=opaque-state&error=access_denied",
        follow_redirects=False,
    )

    _assert_oauth_redirect(
        response,
        "oauth=error&reason=authorization_cancelled&extension_page=oauth_help#authorization-denied",
    )
    assert "unsafe" not in response.headers["location"]


def test_oauth_callback_offloads_sync_handler_and_awaits_async_handler() -> None:
    def registration(fake: FakeCallbackLogger):
        fake.extension["compiled_manifest"]["callbacks"] = [
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": ["GET"],
                "connection_scoped": False,
            }
        ]
        return ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
            extension_id="fixture.callback-provider",
            callback_id="oauth_authorization",
            project_id=fake.project_id,
            base_url="https://canonical.example.test",
        )

    sync_fake = FakeCallbackLogger()
    sync_registration = registration(sync_fake)
    sync_loop_threads: list[int] = []
    sync_handler_threads: list[int] = []

    def sync_handler(**_values: Any) -> dict[str, str]:
        sync_handler_threads.append(threading.get_ident())
        return {"outcome": "connected", "connection_id": "sync-connection"}

    sync_response = _client(
        sync_fake,
        oauth_authorization_callback=sync_handler,
        request_thread_ids=sync_loop_threads,
    ).get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{sync_registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )
    _assert_oauth_redirect(sync_response, "oauth=success&oauth_connection_id=sync-connection")
    assert sync_handler_threads[0] != sync_loop_threads[0]

    async_fake = FakeCallbackLogger()
    async_registration = registration(async_fake)
    async_loop_threads: list[int] = []
    async_handler_threads: list[int] = []

    async def async_handler(**_values: Any) -> dict[str, str]:
        await asyncio.sleep(0)
        async_handler_threads.append(threading.get_ident())
        return {"outcome": "connected", "connection_id": "async-connection"}

    async_response = _client(
        async_fake,
        oauth_authorization_callback=async_handler,
        request_thread_ids=async_loop_threads,
    ).get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{async_registration.callback_token}?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )
    _assert_oauth_redirect(async_response, "oauth=success&oauth_connection_id=async-connection")
    assert async_handler_threads[0] == async_loop_threads[0]


@pytest.mark.parametrize(
    "query",
    [
        "state=one&state=two&code=code",
        "state=one&code=one&code=two",
        "state=one&error=one&error=two",
        "state=one&code=code&error=denied",
        "state=one",
        "code=code",
        f"state={'s' * 257}&code=code",
        f"state={'💥' * 65}&code=code",
        f"state=state&code={'c' * 8193}",
        f"state=state&code={'💥' * 2049}",
        f"state=state&error={'💥' * 33}",
        "&".join(["state=state", "code=code", *[f"x{i}=v" for i in range(15)]]),
    ],
)
def test_oauth_callback_rejects_ambiguous_or_oversized_query(query: str) -> None:
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"] = [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    registration = ExtensionCallbackRegistrationService(fake).ensure_oauth_registration(
        extension_id="fixture.callback-provider",
        callback_id="oauth_authorization",
        project_id=fake.project_id,
        base_url="https://canonical.example.test",
    )
    invoked: list[dict[str, Any]] = []
    client = _client(
        fake,
        oauth_authorization_callback=lambda **values: invoked.append(values),
    )
    response = client.get(
        "/webhook/extensions/fixture.callback-provider/oauth_authorization/"
        f"{registration.callback_token}?{query}",
        follow_redirects=False,
    )
    _assert_oauth_redirect(response, "oauth=error&reason=restart_required")
    assert invoked == []


def test_create_registration_requires_connection_for_connection_scoped_callback():
    fake = FakeCallbackLogger()
    fake.extension["compiled_manifest"]["callbacks"][0]["connection_scoped"] = True
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "connection_id is required for this callback"
    assert fake.registrations == {}
    assert fake.secrets == {}


def test_create_registration_rejects_cross_account_project():
    fake = FakeCallbackLogger()
    fake.project_account_id = "acc-other"
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 403
    assert fake.registrations == {}


def test_create_registration_rejects_extension_from_different_account():
    fake = FakeCallbackLogger()
    fake.project_account_id = "acc-project"
    fake.extension_account_id = "acc-extension"
    client = _client(fake, account_id="acc-project")

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 400
    assert "does not declare callback" in response.json()["error"]
    assert fake.registrations == {}


def test_base_url_from_request_prefers_forwarded_https_origin(monkeypatch):
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://localhost:8099")
    app = FastAPI()

    @app.get("/_probe")
    async def _probe(request: Request):
        return {"base_url": _base_url_from_request(request)}

    client = TestClient(app)
    response = client.get(
        "/_probe",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "powder-skating-actors-merchandise.trycloudflare.com",
        },
    )

    assert response.status_code == 200
    assert (
        response.json()["base_url"] == "https://powder-skating-actors-merchandise.trycloudflare.com"
    )


def test_base_url_from_request_prefers_public_fs_base_url(monkeypatch):
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://public.example.test")
    app = FastAPI()

    @app.get("/_probe")
    async def _probe(request: Request):
        return {"base_url": _base_url_from_request(request)}

    client = TestClient(app)
    response = client.get(
        "/_probe",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "forwarded.example.test",
        },
    )

    assert response.status_code == 200
    assert response.json()["base_url"] == "https://public.example.test"


def test_list_callbacks_json_encodes_datetime_fields():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()
    fake.registrations[created["id"]]["last_seen_at"] = datetime(2026, 5, 10, 9, 30, tzinfo=UTC)

    response = client.get(
        "/api/extensions/fixture.callback-provider/callbacks?project_id=00000000-0000-0000-0000-000000000003"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["callbacks"][0]["last_seen_at"] == "2026-05-10T09:30:00+00:00"


def test_list_callbacks_requires_project_id():
    fake = FakeCallbackLogger()
    client = _client(fake)
    client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )

    response = client.get("/api/extensions/fixture.callback-provider/callbacks")

    assert response.status_code == 400
    assert response.json()["error"] == "project_id is required"


def test_list_callbacks_rejects_cross_account_project():
    fake = FakeCallbackLogger()
    client = _client(fake)
    client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    )
    fake.project_account_id = "acc-other"

    response = client.get(
        "/api/extensions/fixture.callback-provider/callbacks?project_id=00000000-0000-0000-0000-000000000003"
    )

    assert response.status_code == 403


def test_create_registration_with_configure_invokes_action_without_echoing_secrets(monkeypatch):
    # Publishing a callback URL to a provider requires a canonical public base URL.
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://hooks.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fake = FakeCallbackLogger()
    client = _client(fake)
    captured: dict[str, Any] = {}

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "url_configured": True,
                    "pending_update_count": 0,
                },
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "configure": True,
            "configure_action_id": "configure_webhook",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "callback_token" not in body
    assert "callback_secret" not in body
    assert body["callback_url"].endswith("/<redacted>")
    assert body["webhook_status"]["url_configured"] is True
    action_input = captured["input_payload"]
    assert action_input["callback_url"]
    assert action_input["callback_secret"]
    assert action_input["provider"]["provider_config"]["api_token"] == "fixture-token"
    assert action_input["provider"]["project_id"] == "00000000-0000-0000-0000-000000000003"
    assert action_input["provider"]["ingress_config"] == {}


def test_create_registration_with_configure_json_encodes_datetime_payload(monkeypatch):
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://hooks.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fake = FakeCallbackLogger()
    client = _client(fake)

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "url_configured": True,
                    "pending_update_count": 0,
                    "checked_at": datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
                },
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "configure": True,
            "configure_action_id": "configure_webhook",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["webhook_status"]["checked_at"] == "2026-05-10T09:00:00+00:00"


def test_create_registration_with_configure_surfaces_structured_detail(monkeypatch):
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://hooks.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    fake = FakeCallbackLogger()
    client = _client(fake)

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        return {
            "status": "succeeded",
            "response": {
                "ok": False,
                "errors": [
                    {
                        "code": "fixture_set_webhook_failed",
                        "message": "Webhook URL is not reachable.",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "configure": True,
            "configure_action_id": "configure_webhook",
        },
    )

    assert response.status_code == 502
    body = response.json()
    assert body["detail"] == "Webhook URL is not reachable."


def test_configure_rejected_without_canonical_public_base_url(monkeypatch):
    """CAND-009/049: publishing a callback URL to a provider must require a
    configured canonical public base URL; otherwise a spoofed Forwarded/Host
    header could poison the published callback URL (and token)."""
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    invoked: list[Any] = []

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        invoked.append(kwargs)
        return {"status": "succeeded", "response": {"ok": True, "result": {}}}

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    fake = FakeCallbackLogger()
    client = _client(fake)
    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "configure": True,
            "configure_action_id": "configure_webhook",
        },
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "attacker.example.com",
        },
    )

    assert response.status_code == 400
    assert "FS_PUBLIC_BASE_URL" in response.json()["error"]
    # No provider setup happened with a poisoned URL.
    assert invoked == []


def test_configure_publishes_canonical_base_not_forwarded_host(monkeypatch):
    """CAND-009/049: even with a spoofed Forwarded host, the callback URL
    published to the provider must use the configured canonical base URL."""
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://canonical.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    captured: dict[str, Any] = {}

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"status": "succeeded", "response": {"ok": True, "result": {}}}

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    fake = FakeCallbackLogger()
    client = _client(fake)
    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "configure": True,
            "configure_action_id": "configure_webhook",
        },
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "attacker.example.com",
        },
    )

    assert response.status_code == 200
    published = captured["input_payload"]["callback_url"]
    assert published.startswith("https://canonical.example.test/webhook/extensions/")
    assert "attacker.example.com" not in published


def test_callback_provider_status_uses_host_mediated_action(monkeypatch):
    fake = FakeCallbackLogger()
    fake.connections["conn-callback-secondary"] = {
        **fake.connections["conn-callback-primary"],
        "connection_id": "conn-callback-secondary",
        "connection_config": {"destination_id": "-200456"},
    }
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "connection_id": "conn-callback-secondary",
        },
    ).json()
    captured: dict[str, Any] = {}

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "url_configured": True,
                    "pending_update_count": 2,
                    "last_error_message": "",
                },
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    response = client.post(
        f"/api/extensions/fixture.callback-provider/callbacks/{created['id']}/status",
        json={"action_id": "get_webhook_status"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["webhook_status"]["pending_update_count"] == 2
    assert captured["input_payload"]["provider"]["provider_config"]["api_token"] == "fixture-token"
    assert captured["input_payload"]["provider"]["binding_config"]["destination_id"] == "-200456"
    assert captured["input_payload"]["provider"]["connection_id"] == "conn-callback-secondary"
    assert "callback_url" not in captured["input_payload"]
    assert "callback_secret" not in captured["input_payload"]


def test_create_registration_accepts_owned_connection_id():
    fake = FakeCallbackLogger()
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": fake.project_id,
            "connection_id": "conn-callback-primary",
        },
    )

    assert response.status_code == 200
    stored = next(iter(fake.registrations.values()))
    assert stored["connection_id"] == "conn-callback-primary"


def test_create_registration_rejects_connection_from_other_project():
    fake = FakeCallbackLogger()
    fake.connections["conn-foreign-project"] = {
        **fake.connections["conn-callback-primary"],
        "connection_id": "conn-foreign-project",
        "project_id": "00000000-0000-0000-0000-000000000099",
    }
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": fake.project_id,
            "connection_id": "conn-foreign-project",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "connection_id is not valid for this project"
    assert fake.registrations == {}
    assert fake.secrets == {}


def test_create_registration_rejects_connection_from_other_account():
    fake = FakeCallbackLogger()
    fake.connections["conn-foreign-account"] = {
        **fake.connections["conn-callback-primary"],
        "connection_id": "conn-foreign-account",
        "account_id": "acc-other",
    }
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": fake.project_id,
            "connection_id": "conn-foreign-account",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "connection_id is not valid for this account"
    assert fake.registrations == {}
    assert fake.secrets == {}


def test_create_registration_rejects_connection_from_other_extension():
    fake = FakeCallbackLogger()
    fake.connections["conn-foreign-extension"] = {
        **fake.connections["conn-callback-primary"],
        "connection_id": "conn-foreign-extension",
        "extension_id": "acme.other",
    }
    client = _client(fake)

    response = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": fake.project_id,
            "connection_id": "conn-foreign-extension",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "connection_id does not belong to this extension"
    assert fake.registrations == {}
    assert fake.secrets == {}


def test_unknown_callback_token_returns_404():
    fake = FakeCallbackLogger()
    client = _client(fake)

    response = client.post("/webhook/extensions/fixture.callback-provider/updates/nope")

    assert response.status_code == 404


def test_callback_path_extension_mismatch_returns_404():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.post(f"/webhook/extensions/slack/updates/{created['callback_token']}")

    assert response.status_code == 404


def test_callback_path_callback_mismatch_returns_404():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/other/{created['callback_token']}"
    )

    assert response.status_code == 404


def test_disabled_callback_registration_returns_410():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "enabled": False,
        },
    ).json()

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    )

    assert response.status_code == 410


def test_callback_dispatch_rejects_registration_extension_from_different_account():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()
    fake.extension_account_id = "acc-other"

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    )

    assert response.status_code == 404
    assert response.json()["error"] == "Extension not found"


def test_method_mismatch_returns_405():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.get(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    )

    assert response.status_code == 405


def test_missing_shared_secret_returns_401():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    )

    assert response.status_code == 401


def test_mismatched_shared_secret_returns_401():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}",
        headers={"X-Fixture-Callback-Secret": "wrong-secret"},
    )

    assert response.status_code == 401


def test_hmac_callback_rejects_invalid_expired_and_replayed_delivery(monkeypatch):
    fake = FakeCallbackLogger()
    callback = fake.extension["compiled_manifest"]["callbacks"][0]
    callback["auth"] = {
        "type": "hmac_sha256",
        "signature_header": "X-Fixture-Signature",
        "timestamp_header": "X-Fixture-Timestamp",
        "delivery_id_header": "X-Fixture-Delivery",
        "max_age_seconds": 300,
    }
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {"ok": True, "result": {"accepted": True}, "errors": []},
        },
    )
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": fake.project_id, "connection_id": "conn-callback-primary"},
    ).json()
    body = b'{"event_type":"external.comment.received","source":"test"}'

    def headers(timestamp: int, delivery_id: str, *, secret: str | None = None) -> dict[str, str]:
        key = str(secret or created["callback_secret"]).encode()
        message = f"{timestamp}.{delivery_id}.".encode() + body
        signature = hmac.new(key, message, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Fixture-Timestamp": str(timestamp),
            "X-Fixture-Delivery": delivery_id,
            "X-Fixture-Signature": f"sha256={signature}",
        }

    path = f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    now = int(time.time())
    invalid = client.post(path, content=body, headers=headers(now, "delivery-invalid", secret="x"))
    expired = client.post(path, content=body, headers=headers(now - 301, "delivery-expired"))
    accepted = client.post(path, content=body, headers=headers(now, "delivery-1"))
    replayed = client.post(path, content=body, headers=headers(now, "delivery-1"))

    assert invalid.status_code == 401
    assert expired.status_code == 401
    assert accepted.status_code == 200
    assert replayed.status_code == 409
    assert len(fake.callback_deliveries) == 1


def test_rotating_callback_token_invalidates_old_token():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    rotated = client.post(
        f"/api/extensions/fixture.callback-provider/callbacks/{created['id']}/rotate",
        json={},
    ).json()

    assert rotated["callback_token"]
    assert rotated["callback_token"] != created["callback_token"]
    assert (
        client.post(
            f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
        )
    ).status_code == 404


def test_rotate_registration_rejects_cross_account_project():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()
    original_hash = fake.registrations[created["id"]]["callback_token_hash"]
    fake.project_account_id = "acc-other"

    response = client.post(
        f"/api/extensions/fixture.callback-provider/callbacks/{created['id']}/rotate", json={}
    )

    assert response.status_code == 403
    assert fake.registrations[created["id"]]["callback_token_hash"] == original_hash


def test_rotate_wrong_extension_path_does_not_invalidate_token():
    fake = FakeCallbackLogger()
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    response = client.post(f"/api/extensions/slack/callbacks/{created['id']}/rotate", json={})

    assert response.status_code == 404
    assert (
        client.post(
            f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
        )
    ).status_code == 401


def test_valid_shared_secret_dispatches_action_and_redacts_token(monkeypatch):
    fake = FakeCallbackLogger()
    tenant_account_id = "00000000-0000-0000-0000-000000000042"
    fake.project_account_id = tenant_account_id
    fake.extension_account_id = tenant_account_id
    fake.connections["conn-callback-primary"]["account_id"] = tenant_account_id
    fake.connections["conn-callback-secondary"] = {
        **fake.connections["conn-callback-primary"],
        "connection_id": "conn-callback-secondary",
        "connection_config": {"destination_id": "-200456"},
    }
    event_command_service = SimpleNamespace(ingest_provider_events=MagicMock())
    client = _client(
        fake,
        account_id=tenant_account_id,
        event_command_service_factory=lambda: event_command_service,
    )
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "connection_id": "conn-callback-secondary",
        },
    ).json()
    captured: dict[str, Any] = {}

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"project_id": "00000000-0000-0000-0000-000000000003"},
                "events": [
                    {
                        "event_type": "notification.interaction_received",
                        "summary": "received",
                        "project_id": "00000000-0000-0000-0000-000000000003",
                        "payload": {"update_id": 123},
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )
    public_client = _client(
        fake,
        account_id=DEFAULT_ACCOUNT_ID,
        event_command_service_factory=lambda: event_command_service,
    )
    response = public_client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}?a=1",
        headers={"X-Fixture-Callback-Secret": created["callback_secret"]},
        json={"update_id": 123},
    )

    assert response.status_code == 200
    envelope = captured["input_payload"]
    assert envelope["request"]["query"] == {"a": "1"}
    assert envelope["request"]["json"] == {"update_id": 123}
    assert envelope["request"]["body_text"]
    assert envelope["request"]["raw_body_base64"]
    assert envelope["request"]["provider"]["provider_config"]["api_token"] == "fixture-token"
    assert envelope["request"]["provider"]["binding_config"]["destination_id"] == "-200456"
    assert envelope["request"]["provider"]["connection_id"] == "conn-callback-secondary"
    assert captured["runtime_context"]["extension_install_id"] == (
        "install-fixture-callback-provider"
    )
    assert captured["runtime_context"]["account_id"] == tenant_account_id
    assert captured["scope"].account_id == tenant_account_id
    assert captured["scope"].project_id == fake.project_id
    assert created["callback_token"] not in envelope["request"]["path"]
    assert envelope["request"]["headers"]["x-fixture-callback-secret"] == "<redacted>"
    assert response.json()["events_emitted"] == 1
    event_command_service.ingest_provider_events.assert_called_once()
    event_input = event_command_service.ingest_provider_events.call_args.args[0]
    assert event_input.provider_key == "fixture.callback-provider"
    assert event_input.project_id == fake.project_id
    assert event_input.events[0]["event_type"] == "notification.interaction_received"


def test_callback_dispatch_applies_project_state_updates(monkeypatch):
    from core.application.extensions import response_envelope

    fake = FakeCallbackLogger()
    tenant_account_id = "00000000-0000-0000-0000-000000000042"
    fake.project_account_id = tenant_account_id
    fake.extension_account_id = tenant_account_id
    fake.connections["conn-callback-primary"]["account_id"] = tenant_account_id
    fake.extension["compiled_manifest"]["project_state"] = {
        "writes": [{"namespace": "webhook_state", "state_key": "last_update"}]
    }
    client = _client(fake, account_id=tenant_account_id)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000003",
            "connection_id": "conn-callback-primary",
        },
    ).json()
    captured: dict[str, Any] = {}

    def fake_run_extension_action(_extension: dict, **_kwargs: Any) -> dict:
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {},
                "project_state_updates": [
                    {
                        "namespace": "webhook_state",
                        "state_key": "last_update",
                        "value_jsonb": {"update_id": 123},
                    }
                ],
            },
        }

    def fake_put_state(*_args: Any, **kwargs: Any) -> dict:
        captured.update(kwargs)
        return {"revision": 1, **kwargs}

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )
    monkeypatch.setattr(response_envelope.extension_project_state, "put_state", fake_put_state)

    public_client = _client(fake, account_id=DEFAULT_ACCOUNT_ID)
    response = public_client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}",
        headers={"X-Fixture-Callback-Secret": created["callback_secret"]},
        json={"update_id": 123},
    )

    assert response.status_code == 200
    assert captured["account_id"] == tenant_account_id
    assert captured["project_id"] == "00000000-0000-0000-0000-000000000003"
    assert captured["extension_id"] == "fixture.callback-provider"
    assert captured["extension_install_id"] == "install-fixture-callback-provider"
    assert captured["connection_id"] == "conn-callback-primary"
    assert captured["namespace"] == "webhook_state"


def test_callback_host_mutation_failure_blocks_decision_event(monkeypatch):
    fake = FakeCallbackLogger()
    event_command_service = SimpleNamespace(ingest_provider_events=MagicMock())
    client = _client(
        fake,
        event_command_service_factory=lambda: event_command_service,
    )
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": fake.project_id},
    ).json()

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {
                "ok": True,
                "host_mutations": [{"kind": "notification_routes_save", "payload": {}}],
                "events": [
                    {
                        "event_type": "notification.interaction_received",
                        "payload": {"job_id": "job-1", "approved": True},
                    }
                ],
            },
        },
    )

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}",
        headers={"X-Fixture-Callback-Secret": created["callback_secret"]},
        json={"callback_query": {"id": "cb-1"}},
    )

    assert response.status_code == 502
    event_command_service.ingest_provider_events.assert_not_called()


def test_callback_events_cannot_override_registration_project(monkeypatch):
    fake = FakeCallbackLogger()
    client = _client(fake)
    registration_project_id = "00000000-0000-0000-0000-000000000003"
    attacker_project_id = "00000000-0000-0000-0000-000000000099"
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": registration_project_id},
    ).json()
    routed: dict[str, Any] = {}

    def fake_run_extension_action(_extension: dict, **_kwargs: Any) -> dict:
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"project_id": attacker_project_id},
                "events": [
                    {
                        "event_type": "notification.interaction_received",
                        "summary": "received",
                        "project_id": attacker_project_id,
                        "payload": {"update_id": 123},
                    }
                ],
            },
        }

    def fake_route(**kwargs: Any) -> bool:
        routed.update(kwargs)
        return True

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )
    monkeypatch.setattr(
        "core.application.management.command_services._route_extension_event_through_event_command_service",
        fake_route,
    )

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}",
        headers={"X-Fixture-Callback-Secret": created["callback_secret"]},
        json={"update_id": 123},
    )

    assert response.status_code == 200
    assert routed["event"]["project_id"] == registration_project_id
    assert routed["runtime_context"]["project_id"] == registration_project_id
    assert routed["scope"].project_id == registration_project_id


@pytest.mark.parametrize(
    "callback_payload",
    [
        {"message": {"text": "forged reject prompt reply"}},
        {"message": {"text": "expired reject prompt reply"}},
        {"message": {"text": "replayed reject prompt reply"}},
    ],
)
def test_invalid_reject_comment_context_is_acknowledged_without_retry(
    monkeypatch, callback_payload
):
    fake = FakeCallbackLogger()
    event_command_service = SimpleNamespace(ingest_provider_events=MagicMock())
    client = _client(
        fake,
        event_command_service_factory=lambda: event_command_service,
    )
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": "00000000-0000-0000-0000-000000000003"},
    ).json()

    def fake_run_extension_action(extension: dict, **kwargs: Any) -> dict:
        return {
            "status": "succeeded",
            "response": {
                "ok": False,
                "error_code": "invalid_reject_comment_context",
                "error": "Reply to the current chat rejection prompt.",
                "errors": [
                    {
                        "code": "invalid_reject_comment_context",
                        "message": "Reply to the current chat rejection prompt.",
                    }
                ],
                "events": [],
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        fake_run_extension_action,
    )

    response = client.post(
        f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}",
        headers={"X-Fixture-Callback-Secret": created["callback_secret"]},
        json=callback_payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["events"] == []
    assert body["errors"][0]["code"] == "invalid_reject_comment_context"
    assert body["errors"][0]["message"] == "Reply to the current chat rejection prompt."
    event_command_service.ingest_provider_events.assert_not_called()
    assert fake.registrations[created["id"]]["last_error"] == ""


APP_SECRET_NAME = "app_secret"  # pragma: allowlist secret


def _hub_signature_setup(monkeypatch, *, app_secret_value: str = "meta-app-value"):
    fake = FakeCallbackLogger()
    callback = fake.extension["compiled_manifest"]["callbacks"][0]
    callback["methods"] = ["GET", "POST"]
    callback["auth"] = {
        "type": "hub_signature",
        "signature_header": "X-Hub-Signature-256",
        "secret_name": APP_SECRET_NAME,
    }
    fake.connections["conn-callback-primary"]["secret_refs"] = {
        "provider_secrets": {
            "provider_key": "fixture.callback-provider",
            "keys": {APP_SECRET_NAME: APP_SECRET_NAME},
        }
    }

    def get_provider_secret(**kwargs: Any) -> str:
        return app_secret_value if kwargs.get("secret_key") == APP_SECRET_NAME else ""

    fake.get_provider_secret = get_provider_secret
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "response": {"ok": True, "result": {"accepted": True}, "errors": []},
        },
    )
    client = _client(fake)
    created = client.post(
        "/api/extensions/fixture.callback-provider/callbacks/updates",
        json={"project_id": fake.project_id, "connection_id": "conn-callback-primary"},
    ).json()
    path = f"/webhook/extensions/fixture.callback-provider/updates/{created['callback_token']}"
    return client, created, path, app_secret_value


def test_hub_signature_callback_answers_the_subscription_handshake(monkeypatch):
    client, created, path, _ = _hub_signature_setup(monkeypatch)
    verify_token = created["callback_secret"]

    accepted = client.get(
        path,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": verify_token,
            "hub.challenge": "1158201444",
        },
    )
    wrong_token = client.get(
        path,
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    )
    wrong_mode = client.get(
        path,
        params={"hub.mode": "unsubscribe", "hub.verify_token": verify_token, "hub.challenge": "1"},
    )
    no_challenge = client.get(
        path, params={"hub.mode": "subscribe", "hub.verify_token": verify_token}
    )

    assert accepted.status_code == 200
    assert accepted.text == "1158201444"
    assert accepted.headers["content-type"].startswith("text/plain")
    assert wrong_token.status_code == 403
    assert wrong_mode.status_code == 403
    assert no_challenge.status_code == 403


def test_hub_signature_callback_checks_the_raw_body_against_the_app_secret(monkeypatch):
    client, created, path, app_secret_value = _hub_signature_setup(monkeypatch)
    body = b'{"object":"whatsapp_business_account","entry":[]}'

    def sign(secret: str, payload: bytes = body) -> dict[str, str]:
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return {"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"}

    accepted = client.post(path, content=body, headers=sign(app_secret_value))
    wrong_key = client.post(path, content=body, headers=sign("not-the-app-secret"))
    # The registration's own secret must not be accepted as the signing key.
    registration_key = client.post(path, content=body, headers=sign(created["callback_secret"]))
    tampered = client.post(path, content=body + b" ", headers=sign(app_secret_value))
    unsigned = client.post(path, content=body, headers={"Content-Type": "application/json"})

    assert accepted.status_code == 200
    assert wrong_key.status_code == 401
    assert registration_key.status_code == 401
    assert tampered.status_code == 401
    assert unsigned.status_code == 401


def test_hub_signature_callback_without_a_stored_app_secret_rejects_every_post(monkeypatch):
    client, _created, path, _ = _hub_signature_setup(monkeypatch, app_secret_value="")
    body = b"{}"
    digest = hmac.new(b"", body, hashlib.sha256).hexdigest()

    response = client.post(
        path,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"},
    )

    assert response.status_code == 401


def test_every_callback_auth_type_is_allowed_by_the_registrations_table():
    """The table's CHECK must list each auth type the runtime accepts.

    ``hub_signature`` once passed validation and then failed on insert, because the
    constraint in ``db/schema`` still named the three older types.
    """
    from pathlib import Path

    from core.infrastructure.extension_runtime.extension_runtime_common import (
        ALLOWED_CALLBACK_AUTH_TYPES,
    )

    schema = Path(__file__).resolve().parents[2] / "db" / "schema" / "001_schema_part_01.sql"
    constraint = next(
        line
        for line in schema.read_text().splitlines()
        if "extension_callback_registrations_auth_type_check" in line
    )
    for auth_type in ALLOWED_CALLBACK_AUTH_TYPES:
        assert f"'{auth_type}'::text" in constraint
