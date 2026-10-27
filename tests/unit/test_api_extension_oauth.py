from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.extensions import callbacks as extension_callbacks
from core.application.extensions.oauth_authorization import (
    OAuthAuthorizationStartResult,
    OAuthRedirectURIResult,
)
from core.application.extensions.oauth_configuration import (
    OAuthConfigurationFieldView,
    OAuthConfigurationImpact,
    OAuthConfigurationView,
)
from core.application.management.errors import (
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.extension_oauth import create_extension_oauth_router
from core.infrastructure.db.db_pool import PooledConnectionProxy
from core.infrastructure.runtime.extension_oauth_db_adapters import (
    create_extension_oauth_authorization_service,
    create_extension_oauth_callback_service,
    create_extension_oauth_configuration_service,
    create_extension_oauth_connection_lifecycle_service,
)


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.authorization_service = FakeAuthorizationService()
        self.callback_service = FakeCallbackService()

    def list_configurations(self, scope, install_id, provider_id):
        self.calls.append(("list", scope))
        fields = (
            [OAuthConfigurationFieldView(field_id="client_secret", secret_present=True)]
            if scope.principal_type != "viewer"
            else []
        )
        return [
            OAuthConfigurationView(
                configuration_id="cfg-1",
                display_name="Primary",
                status="ready_for_authorization",
                fields=fields,
                revision=1,
                authorization_revision=1,
            )
        ]

    def preview_configuration_change(self, scope, install_id, provider_id, command, **kwargs):
        self.calls.append(("preview", command))
        return OAuthConfigurationImpact(
            configuration_id=command.configuration_id,
            expected_revision=kwargs["expected_revision"],
            changed_fields=["client_id"],
            change_policies=["reauthorization_required"],
            connections=[],
            requires_confirmation=False,
        )

    def save_configuration(self, scope, install_id, provider_id, command, **kwargs):
        self.calls.append(("save", command))
        if command.values.get("client_id") == "bad":
            raise ServiceValidationError(
                "client_id is too short",
                payload={
                    "error": "invalid_credential_field",
                    "field_id": "client_id",
                    "reason": "too_short",
                },
            )
        return self.list_configurations(scope, install_id, provider_id)[0]

    def preview_configuration_delete(self, *args, **kwargs):
        return OAuthConfigurationImpact(
            configuration_id=args[3],
            expected_revision=kwargs["expected_revision"],
            changed_fields=["__delete__"],
            change_policies=[],
            connections=[],
            requires_confirmation=False,
        )

    def delete_configuration(self, *args, **kwargs):
        self.calls.append(("delete", args[3]))

    def reconcile_release(self, scope, install_id, provider_id, configuration_id):
        self.calls.append(("reconcile", configuration_id))
        return OAuthConfigurationView(
            configuration_id=configuration_id,
            display_name="Primary",
            status="verified",
            fields=[],
            revision=3,
            authorization_revision=1,
        )

    def disconnect(self, scope, install_id, provider_id, configuration_id, connection_id):
        self.calls.append(("disconnect", (scope, configuration_id, connection_id)))
        return {
            "status": "disconnected",
            "connection_id": connection_id,
            "remote_revocation": "failed",
        }


class FakeAuthorizationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.registration_available = True

    def _require_registration(self) -> None:
        if not self.registration_available:
            raise ServicePreconditionError(
                "OAuth callback registration is unavailable",
                payload={"error": "oauth_callback_registration_unavailable"},
            )

    def ensure_redirect_uri(self, scope, install_id, provider_id):
        self.calls.append(("ensure", scope))
        self._require_registration()
        return OAuthRedirectURIResult(
            redirect_uri=(
                "https://canonical.example.test/webhook/extensions/fixture.extension-oauth/"
                "oauth_authorization/stable-token"
            )
        )

    def provider_setup_status(self, scope, install_id, provider_id):
        self.calls.append(("status", scope))
        return SimpleNamespace(model_dump=lambda **_kwargs: {"callback_registration_ready": True})

    def start_authorization(self, scope, install_id, provider_id, command):
        self.calls.append(("start", command))
        self._require_registration()
        redirect_uri = self.ensure_redirect_uri(scope, install_id, provider_id).redirect_uri
        return OAuthAuthorizationStartResult(
            authorization_url="https://identity.example.test/authorize?state=opaque",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            redirect_uri=redirect_uri,
        )


class FakeCallbackService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.outcome = "connected"

    def retry_post_connect(self, **values):
        self.calls.append(values)
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "outcome": self.outcome,
                "connection_id": values["connection_id"],
            }
        )


def _client(*, role="operator", account="acct-1", project_account="acct-1"):
    service = FakeService()
    logger = SimpleNamespace(
        get_project=lambda project_id: {"id": project_id, "account_id": project_account},
        get_extension_install_for_operator=lambda install_id, **_kwargs: {
            "install_id": install_id,
            "extension_id": "fixture.extension-oauth",
        },
    )
    app = FastAPI()

    def auth(_request, *, required_roles=None):
        if required_roles and role not in required_roles:
            raise PermissionError("denied")
        return {"roles": [role]}

    app.include_router(
        create_extension_oauth_router(
            get_logger_only=lambda: logger,
            request_scope=lambda _request, project_id: ScopeContext(
                account_id=account,
                tenant_id="tenant-1",
                project_id=project_id,
                principal_type=role,
            ),
            require_authenticated_user=auth,
            service_factory=lambda _logger: service,
            authorization_service_factory=lambda _logger: service.authorization_service,
            callback_service_factory=lambda _logger: service.callback_service,
            connection_lifecycle_service_factory=lambda _logger: service,
            service_error_response=lambda exc: JSONResponse(
                exc.payload, status_code=exc.status_code
            ),
            api_error_response=lambda exc, **_kwargs: JSONResponse(
                {"error": "internal"}, status_code=500
            ),
        )
    )
    return TestClient(app), service


BASE = "/api/projects/project-1/extensions/install-1/oauth/example/configurations"
CONFIGURATION_ID = "10000000-0000-0000-0000-000000000001"
OAUTH_BASE = "/api/projects/project-1/extensions/install-1/oauth/example"


def test_generic_disconnect_route_is_project_scoped_and_returns_redacted_outcome() -> None:
    client, service = _client()
    response = client.post(
        f"{OAUTH_BASE}/configurations/{CONFIGURATION_ID}/connections/connection-1/disconnect"
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "disconnected",
        "connection_id": "connection-1",
        "remote_revocation": "failed",
    }
    kind, values = service.calls[-1]
    assert kind == "disconnect"
    assert values[0].project_id == "project-1"


def test_viewer_list_is_redacted_and_mutation_is_forbidden() -> None:
    client, _service = _client(role="viewer")
    listed = client.get(BASE)
    assert listed.status_code == 200
    assert listed.json()[0]["fields"] == []
    assert "secret_present" not in listed.text
    assert "ref" not in listed.text
    denied = client.put(
        BASE,
        json={
            "display_name": "Primary",
            "values": {"client_id": "abc"},
            "secret_replacements": {"client_secret": "secret"},
        },
    )
    assert denied.status_code == 403


def test_operator_routes_map_safe_field_validation_payload() -> None:
    client, _service = _client()
    response = client.put(
        BASE,
        json={"display_name": "Primary", "values": {"client_id": "bad"}},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_credential_field",
        "field_id": "client_id",
        "reason": "too_short",
    }


def test_foreign_project_access_is_concealed_as_not_found_before_service() -> None:
    client, service = _client(account="acct-1", project_account="acct-2")
    response = client.get(BASE)
    assert response.status_code == 404
    assert response.json() == {"error": "Project not found"}
    assert service.calls == []


def test_delete_and_impact_routes_are_typed_and_operator_only() -> None:
    client, service = _client()
    impact = client.post(f"{BASE}/{CONFIGURATION_ID}/delete-impact", json={"expected_revision": 1})
    assert impact.status_code == 200
    assert impact.json()["changed_fields"] == ["__delete__"]
    deleted = client.request("DELETE", f"{BASE}/{CONFIGURATION_ID}", json={"expected_revision": 1})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert ("delete", CONFIGURATION_ID) in service.calls


def test_unknown_top_level_request_fields_are_rejected_before_service() -> None:
    client, service = _client()
    response = client.put(BASE, json={"display_name": "Primary", "surprise": True})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert service.calls == []


def test_malformed_secret_input_is_never_reflected_in_validation_response() -> None:
    client, service = _client()
    canary = "must-never-return-secret-canary"
    response = client.put(
        BASE,
        json={
            "display_name": "Primary",
            "secret_replacements": {"client_secret": [canary]},
        },
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert canary not in response.text
    assert service.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"display_name":"First","display_name":"root-canary"}',
        '{"display_name":"Primary","values":{"client_id":"first","client_id":"value-canary"}}',
        '{"display_name":"Primary","secret_replacements":{"client_secret":"first","client_secret":"secret-canary"}}',
    ],
)
def test_configuration_routes_reject_duplicate_json_keys_without_echo(raw: str) -> None:
    client, service = _client()

    response = client.put(BASE, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "canary" not in response.text
    assert service.calls == []


def test_credential_route_does_not_expose_authorization_duplicate_error_contract() -> None:
    client, service = _client()
    raw = (
        '{"display_name":"Primary","intent_inputs":'
        '{"email_hint":"first@example.test","email_hint":"route-canary"}}'
    )

    response = client.put(BASE, content=raw, headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "route-canary" not in response.text
    assert service.calls == []


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_oauth_body_routes_reject_nonstandard_json_constants(constant: str) -> None:
    client, service = _client()

    response = client.put(
        BASE,
        content=f'{{"display_name":"Primary","values":{{"client_id":{constant}}}}}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert service.calls == []


def test_top_level_non_object_secret_input_is_never_reflected() -> None:
    client, service = _client()
    canary = "top-level-secret-canary"
    response = client.put(BASE, json=[canary])
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert canary not in response.text
    assert service.calls == []


def test_invalid_json_is_a_closed_non_reflecting_error() -> None:
    client, service = _client()
    canary = "broken-json-secret-canary"
    response = client.put(
        BASE,
        content='{"secret_replacements":{"client_secret":"' + canary,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert canary not in response.text
    assert service.calls == []


def test_operator_can_ensure_exact_redirect_uri_without_client_credentials() -> None:
    client, service = _client()
    response = client.put(
        f"{OAUTH_BASE}/callback-registration",
        headers={"host": "attacker.example.test", "x-forwarded-host": "attacker.example.test"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "redirect_uri": (
            "https://canonical.example.test/webhook/extensions/fixture.extension-oauth/"
            "oauth_authorization/stable-token"
        )
    }
    assert "attacker.example.test" not in response.text
    assert service.authorization_service.calls[0][0] == "ensure"


def test_viewer_can_read_callback_setup_status_without_mutating_registration() -> None:
    client, service = _client(role="viewer")

    response = client.get(f"{OAUTH_BASE}/setup-status")

    assert response.status_code == 200
    assert response.json() == {"callback_registration_ready": True}
    assert service.authorization_service.calls[0][0] == "status"


def test_authorization_start_returns_only_bounded_public_fields() -> None:
    client, service = _client()
    response = client.post(
        f"{OAUTH_BASE}/authorization",
        json={
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
            "intent_id": "connect",
            "display_name": "Mailbox",
            "return_page_id": "connections",
            "intent_inputs": {"email_hint": "person@example.test"},
        },
        headers={"host": "attacker.example.test", "x-forwarded-host": "attacker.example.test"},
    )
    assert response.status_code == 200
    assert set(response.json()) == {"authorization_url", "expires_at", "redirect_uri"}
    assert "attacker.example.test" not in response.text
    assert service.authorization_service.calls[0][0] == "start"


def test_authorization_route_accepts_one_click_reconnect_without_intent_inputs() -> None:
    client, service = _client()

    response = client.post(
        f"{OAUTH_BASE}/authorization",
        json={
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
            "intent_id": "connect",
            "display_name": "Mailbox",
            "return_page_id": "connections",
            "intent_inputs": {},
            "existing_connection_id": "connection-1",
        },
    )

    assert response.status_code == 200
    kind, command = service.authorization_service.calls[0]
    assert kind == "start"
    assert command.intent_inputs == {}
    assert command.existing_connection_id == "connection-1"


def test_operator_can_retry_post_connect_without_new_consent() -> None:
    client, service = _client()

    response = client.post(
        f"{OAUTH_BASE}/connections/connection-1/post-connect/retry",
        json={
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"outcome": "connected", "connection_id": "connection-1"}
    assert service.callback_service.calls == [
        {
            "tenant_id": "tenant-1",
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.extension-oauth",
            "provider_id": "example",
            "configuration_id": CONFIGURATION_ID,
            "connection_id": "connection-1",
            "connection_type": "example_mail",
        }
    ]


def test_post_connect_retry_is_operator_only_and_rejects_extra_fields() -> None:
    viewer, viewer_service = _client(role="viewer")
    denied = viewer.post(
        f"{OAUTH_BASE}/connections/connection-1/post-connect/retry",
        json={"configuration_id": CONFIGURATION_ID, "connection_type": "example_mail"},
    )
    assert denied.status_code == 403
    assert viewer_service.callback_service.calls == []

    client, service = _client()
    invalid = client.post(
        f"{OAUTH_BASE}/connections/connection-1/post-connect/retry",
        json={
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
            "refresh_token": "must-not-be-accepted",
        },
    )
    assert invalid.status_code == 400
    assert "must-not-be-accepted" not in invalid.text
    assert service.callback_service.calls == []


@pytest.mark.parametrize("outcome", ["needs_attention", "authorization_required"])
def test_post_connect_retry_api_preserves_closed_lifecycle_outcome(outcome: str) -> None:
    client, service = _client()
    service.callback_service.outcome = outcome

    response = client.post(
        f"{OAUTH_BASE}/connections/connection-1/post-connect/retry",
        json={"configuration_id": CONFIGURATION_ID, "connection_type": "example_mail"},
    )

    assert response.status_code == 200
    assert response.json() == {"outcome": outcome, "connection_id": "connection-1"}


def test_authorization_start_rejects_invalid_uuid_and_unknown_fields_safely() -> None:
    client, service = _client()
    for body in (
        {
            "configuration_id": "x" * 36,
            "connection_type": "example_mail",
            "intent_id": "connect",
            "display_name": "Mailbox",
            "return_page_id": "connections",
        },
        {
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
            "intent_id": "connect",
            "display_name": "Mailbox",
            "return_page_id": "connections",
            "redirect_uri": "https://attacker.example.test/callback",
        },
    ):
        response = client.post(f"{OAUTH_BASE}/authorization", json=body)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_request"}
        assert "attacker.example.test" not in response.text
    assert service.authorization_service.calls == []


def test_authorization_start_rejects_duplicate_intent_input_without_echo() -> None:
    client, service = _client()
    canary = "second-value-must-not-return"
    raw = (
        '{"configuration_id":"'
        + CONFIGURATION_ID
        + '","connection_type":"example_mail","intent_id":"connect",'
        '"display_name":"Mailbox","intent_inputs":'
        '{"email_hint":"first@example.test","email_hint":"' + canary + '"}}'
    )
    response = client.post(
        f"{OAUTH_BASE}/authorization",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "duplicate_oauth_intent_input",
        "field": "email_hint",
    }
    assert canary not in response.text
    assert service.authorization_service.calls == []


def test_oauth_credential_routes_reject_oversize_declared_body_without_echo() -> None:
    client, service = _client()
    canary = "oversize-secret-canary"
    payload = (
        '{"display_name":"Primary","secret_replacements":{"client_secret":"'
        + (canary * 6000)
        + '"}}'
    )
    response = client.put(BASE, content=payload, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}
    assert canary not in response.text
    assert service.calls == []


def test_authorization_route_enforces_stream_cap_without_content_length() -> None:
    client, service = _client()
    canary = "chunked-oauth-canary"

    def chunks():
        yield b'{"configuration_id":"10000000-0000-0000-0000-000000000001",'
        yield b'"connection_type":"example_mail","intent_id":"connect",'
        yield b'"display_name":"Mailbox","intent_inputs":{"email_hint":"'
        yield (canary * 6000).encode()
        yield b'"}}'

    response = client.post(
        f"{OAUTH_BASE}/authorization",
        content=chunks(),
        headers={"content-type": "application/json", "transfer-encoding": "chunked"},
    )
    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}
    assert canary not in response.text
    assert service.authorization_service.calls == []


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("POST", f"/{CONFIGURATION_ID}/delete-impact"),
        ("DELETE", f"/{CONFIGURATION_ID}"),
    ],
)
@pytest.mark.parametrize("chunked", [False, True])
def test_delete_routes_enforce_stream_cap_without_secret_echo(
    method: str,
    suffix: str,
    chunked: bool,
) -> None:
    client, service = _client()
    canary = "oversize-delete-secret-canary"
    payload = '{"expected_revision":1,"confirmation_token":"' + (canary * 5000) + '"}'
    content = (
        (part for part in (payload[:100].encode(), payload[100:].encode())) if chunked else payload
    )
    headers = {"content-type": "application/json"}
    if chunked:
        headers["transfer-encoding"] = "chunked"

    response = client.request(method, f"{BASE}{suffix}", content=content, headers=headers)

    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}
    assert canary not in response.text
    assert service.calls == []


def test_callback_registration_precondition_is_safe_for_ensure_and_start() -> None:
    client, service = _client()
    service.authorization_service.registration_available = False
    ensure = client.put(f"{OAUTH_BASE}/callback-registration")
    started = client.post(
        f"{OAUTH_BASE}/authorization",
        json={
            "configuration_id": CONFIGURATION_ID,
            "connection_type": "example_mail",
            "intent_id": "connect",
            "display_name": "Mailbox",
            "return_page_id": "connections",
        },
    )
    assert ensure.status_code == 412
    assert started.status_code == 412
    assert ensure.json() == started.json() == {"error": "oauth_callback_registration_unavailable"}


def test_production_authorization_factory_maps_missing_canonical_origin_safely(
    monkeypatch,
) -> None:
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    service = create_extension_oauth_authorization_service(SimpleNamespace(conn=object()))
    with pytest.raises(ServiceUnavailableError) as exc_info:
        service._register_redirect_uri(
            project_id="project-1",
            extension_id="fixture.extension-oauth",
            callback_id="oauth_authorization",
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.payload == {
        "error": "oauth_public_base_url_unavailable",
        "reason": "missing",
    }
    assert "Host" not in str(exc_info.value.payload)


@pytest.mark.parametrize(
    ("origin", "resolved", "reason"),
    (
        ("https://app.example.test:443", ["8.8.8.8"], "invalid"),
        ("https://app.example.test", [], "unresolvable"),
        ("https://app.example.test", ["10.0.0.5"], "disallowed"),
    ),
)
def test_production_authorization_reports_safe_public_origin_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    resolved: list[str],
    reason: str,
) -> None:
    from core.application.extensions import url_safety

    monkeypatch.setenv("FS_PUBLIC_BASE_URL", origin)
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")
    monkeypatch.setattr(url_safety, "_resolve_hostname_ips", lambda *_args, **_kwargs: resolved)
    service = create_extension_oauth_authorization_service(SimpleNamespace(conn=object()))
    with pytest.raises(ServiceUnavailableError) as exc_info:
        service._register_redirect_uri(
            project_id="project-1",
            extension_id="fixture.extension-oauth",
            callback_id="oauth_authorization",
        )
    assert exc_info.value.payload == {
        "error": "oauth_public_base_url_unavailable",
        "reason": reason,
    }


def test_production_authorization_reports_dns_failure_without_resolver_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from core.application.extensions import url_safety

    def refuse_resolution(*_args, **_kwargs):
        raise socket.gaierror("internal resolver diagnostic must not reach the browser")

    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")
    monkeypatch.setattr(url_safety, "_resolve_hostname_ips", refuse_resolution)
    service = create_extension_oauth_authorization_service(SimpleNamespace(conn=object()))
    with pytest.raises(ServiceUnavailableError) as exc_info:
        service._register_redirect_uri(
            project_id="project-1",
            extension_id="fixture.extension-oauth",
            callback_id="oauth_authorization",
        )
    assert exc_info.value.payload == {
        "error": "oauth_public_base_url_unavailable",
        "reason": "unresolvable",
    }


@pytest.mark.parametrize(
    "canonical_origin",
    (
        "https://localhost:8099",
        "https://127.0.0.1:8099",
        "https://[::1]:8099",
    ),
)
def test_browser_oauth_canonical_origin_allows_exact_https_loopback_without_private_override(
    monkeypatch: pytest.MonkeyPatch,
    canonical_origin: str,
) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", canonical_origin)
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")

    assert extension_callbacks.browser_oauth_callback_base_url() == canonical_origin


@pytest.mark.parametrize(
    "canonical_origin",
    (
        "",
        "http://localhost:8099",
        "https://operator@localhost:8099",
        "https://localhost:bad",
        "https://localhost:8099/path",
        "https://localhost:8099?next=callback",
        "https://localhost:8099#callback",
        "https://10.0.0.5:8099",
    ),
)
def test_browser_oauth_canonical_origin_fails_closed_for_invalid_or_non_loopback_private_values(
    monkeypatch: pytest.MonkeyPatch,
    canonical_origin: str,
) -> None:
    if canonical_origin:
        monkeypatch.setenv("FS_PUBLIC_BASE_URL", canonical_origin)
    else:
        monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")

    with pytest.raises(extension_callbacks.OAuthCallbackPublicBaseURLUnavailable):
        extension_callbacks.browser_oauth_callback_base_url()


def test_browser_oauth_canonical_origin_ignores_spoofed_request_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FS_PUBLIC_BASE_URL", raising=False)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "https",
            "server": ("attacker.example.test", 443),
            "headers": [
                (b"host", b"attacker.example.test"),
                (b"x-forwarded-host", b"attacker.example.test"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
    )

    with pytest.raises(extension_callbacks.OAuthCallbackPublicBaseURLUnavailable):
        extension_callbacks.browser_oauth_callback_base_url(request)


def test_generic_webhook_provider_setup_still_rejects_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://localhost:8099")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")

    with pytest.raises(extension_callbacks.OAuthCallbackPublicBaseURLUnavailable):
        extension_callbacks.callback_base_url_for_provider_setup()


def test_extension_browser_oauth_factories_share_the_canonical_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_origin = "https://localhost:8099"
    callback_url = (
        f"{canonical_origin}/webhook/extensions/fixture.extension-oauth/"
        "oauth_authorization/stable-token"
    )
    captured_base_urls: list[str] = []

    def ensure_registration(_service, **values):
        captured_base_urls.append(str(values.get("base_url") or ""))
        return SimpleNamespace(callback_url=callback_url)

    monkeypatch.setenv("FS_PUBLIC_BASE_URL", canonical_origin)
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "0")
    monkeypatch.setattr(
        extension_callbacks.ExtensionCallbackRegistrationService,
        "ensure_oauth_registration",
        ensure_registration,
    )
    monkeypatch.setattr(
        extension_callbacks.ExtensionCallbackRegistrationService,
        "oauth_registration_is_ready",
        lambda _service, **_values: True,
    )
    logger = SimpleNamespace(
        conn=object(),
        get_extension_install_for_operator=lambda *_args, **_kwargs: None,
    )

    authorization_service = create_extension_oauth_authorization_service(logger)
    callback_service = create_extension_oauth_callback_service(logger)

    assert (
        authorization_service._register_redirect_uri(
            project_id="project-1",
            extension_id="fixture.extension-oauth",
            callback_id="oauth_authorization",
        )
        == callback_url
    )
    assert callback_service._public_base_url == canonical_origin
    assert captured_base_urls == [canonical_origin]
    assert (
        authorization_service._callback_registration_ready(
            project_id="project-1",
            extension_id="fixture.extension-oauth",
            callback_id="oauth_authorization",
        )
        is True
    )


def test_production_oauth_save_and_callback_factories_pin_pooled_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RawConnection:
        autocommit = True
        closed = 0

        def __init__(self) -> None:
            self.transactions = 0

        def get_transaction_status(self) -> int:
            return 0

        @contextmanager
        def transaction(self):
            self.transactions += 1
            yield self

    class _Pool:
        def __init__(self) -> None:
            self.connection = _RawConnection()
            self.get_calls = 0
            self.put_calls = 0

        def getconn(self):
            self.get_calls += 1
            return self.connection

        def putconn(self, connection, close=False) -> None:
            assert connection is self.connection
            assert close is False
            self.put_calls += 1

    monkeypatch.setenv("FS_SECRET_KEY", "test-oauth-transaction-key")
    monkeypatch.setenv("FS_PUBLIC_BASE_URL", "https://public.example.test")
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    pool = _Pool()
    proxy = PooledConnectionProxy(pool)
    logger = SimpleNamespace(
        conn=proxy,
        get_extension_install_for_operator=lambda *_args, **_kwargs: None,
    )
    services = (
        create_extension_oauth_configuration_service(logger),
        create_extension_oauth_authorization_service(logger),
        create_extension_oauth_callback_service(logger),
        create_extension_oauth_connection_lifecycle_service(logger),
    )

    assert not hasattr(proxy, "transaction")
    for service in services:
        with service._transaction_factory():
            pass

    assert pool.get_calls == len(services)
    assert pool.put_calls == len(services)
    assert pool.connection.transactions == len(services)


def test_reconcile_release_route_moves_the_configuration_and_needs_an_operator() -> None:
    client, service = _client()
    response = client.post(f"{BASE}/{CONFIGURATION_ID}/reconcile-release")
    assert response.status_code == 200
    assert response.json()["revision"] == 3
    assert ("reconcile", CONFIGURATION_ID) in service.calls

    viewer, viewer_service = _client(role="viewer")
    refused = viewer.post(f"{BASE}/{CONFIGURATION_ID}/reconcile-release")
    assert refused.status_code == 403
    assert viewer_service.calls == []
