from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from core.application.extensions.callbacks import (
    OAuthCallbackRegistrationPreconditionError,
)
from core.application.extensions.oauth_authorization import (
    OAuthAuthorizationService,
    OAuthAuthorizationStart,
)
from core.application.management.errors import (
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext


def _descriptor() -> dict[str, Any]:
    return {
        "extension_id": "fixture.extension-oauth",
        "ui_manifest": {
            "pages": [
                {
                    "page_id": "connections",
                    "components": [
                        {
                            "type": "connection_form",
                            "data": {
                                "oauth_provider_id": "example",
                                "connection_type": "example_mail",
                                "oauth_intent": "connect",
                            },
                        }
                    ],
                },
                {"page_id": "oauth_help"},
                {"page_id": "oauth_setup"},
            ]
        },
        "callbacks": [
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": ["GET"],
                "connection_scoped": False,
            }
        ],
        "action_definitions": [{"action_id": "verify_identity", "handler": {"mode": "extension"}}],
        "runtime": {
            "extension_contract_v2": {
                "oauth_providers": [
                    {
                        "provider_id": "example",
                        "display_name": "Example",
                        "description": "Example OAuth provider",
                        "setup_page_id": "oauth_setup",
                        "help_page_id": "oauth_help",
                        "flow": {"type": "authorization_code", "pkce": "s256"},
                        "credential_fields": [
                            {
                                "field_id": "client_id",
                                "value_type": "text",
                                "required": True,
                                "secret": False,
                                "change_policy": "reauthorization_required",
                                "label": "Client ID",
                                "help_text": "Client ID",
                                "validation": {"min_length": 1, "max_length": 512},
                            },
                            {
                                "field_id": "client_secret",
                                "value_type": "secret",
                                "required": True,
                                "secret": True,
                                "change_policy": "runtime_revalidate",
                                "label": "Client secret",
                                "help_text": "Client secret",
                                "validation": {"min_length": 1, "max_length": 4096},
                            },
                        ],
                        "authorization_endpoint": {
                            "url": "https://identity.example.test/authorize"
                        },
                        "token_endpoint": {
                            "url": "https://identity.example.test/token",
                            "client_auth_method": "client_secret_post",
                        },
                        "callback_id": "oauth_authorization",
                        "authorization_intents": [
                            {
                                "intent_id": "connect",
                                "static_parameters": {
                                    "access_type": "offline",
                                    "prompt": "consent select_account",
                                },
                                "user_inputs": [
                                    {
                                        "input_id": "email_hint",
                                        "parameter": "login_hint",
                                        "validation": {"type": "email", "max_length": 320},
                                    }
                                ],
                            }
                        ],
                        "post_connect_action_id": "verify_identity",
                    }
                ],
                "connection_types": [
                    {
                        "connection_type_id": "example_mail",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "example",
                            "scopes": {
                                "required": ["mail.read"],
                                "optional": ["mail.write"],
                            },
                        },
                    }
                ],
            }
        },
    }


def _bound_descriptor() -> dict[str, Any]:
    descriptor = _descriptor()
    provider = descriptor["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    provider["origin_binding"] = {
        "binding_id": "tenant_origin",
        "input_id": "tenant_domain",
        "host_suffix": ".identity.example.test",
        "label": {
            "charset": "lowercase_alnum_hyphen",
            "min_length": 1,
            "max_length": 60,
        },
    }
    provider["authorization_endpoint"] = {
        "origin_binding": "tenant_origin",
        "path": "/oauth/authorize",
    }
    provider["token_endpoint"] = {
        "origin_binding": "tenant_origin",
        "path": "/oauth/token",
        "client_auth_method": "client_secret_post",
    }
    provider["callback_verification"] = {
        "signature_parameter": "signature",
        "algorithm": "hmac_sha256",
        "digest": "hex",
        "message": "sorted_query_excluding_signature",
        "secret_source": "client_secret",
        "subject_parameter": "tenant",
        "subject_binding": "tenant_origin",
    }
    provider["authorization_intents"][0]["user_inputs"] = [
        {
            "input_id": "tenant_domain",
            "origin_binding": "tenant_origin",
            "validation": {"type": "hostname_label", "max_length": 80},
        }
    ]
    return descriptor


class _Repository:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.states: list[dict[str, Any]] = []
        self.configuration = {
            "configuration_id": "11111111-1111-4111-8111-111111111111",
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "extension_release_id": "release-a",
            "provider_id": "example",
            "configuration_revision": 3,
            "authorization_revision": 2,
            "status": "ready_for_authorization",
            "non_secret_config": {"client_id": "declared-client"},
            "secret_refs": {
                "refs": {
                    "client_secret": (
                        "extension_oauth_configuration:"
                        "11111111-1111-4111-8111-111111111111:client_secret"
                    )
                }
            },
            "provider_security_fingerprint": "",
        }
        self.connection = {
            "connection_id": "connection-a",
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "oauth_provider_id": "example",
            "oauth_configuration_id": "11111111-1111-4111-8111-111111111111",
            "oauth_extension_release_id": "release-a",
            "oauth_provider_security_fingerprint": "",
            "connection_type": "example_mail",
            "credential_revision": 7,
            "enabled": True,
            "connection_config": {"oauth": {"lifecycle_status": "connected"}},
        }

    def get_configuration_for_scope_for_update(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("lock_configuration")
        return dict(self.configuration)

    def lock_connection(self, **_scope: Any) -> dict[str, Any] | None:
        self.events.append("lock_connection")
        return dict(self.connection)

    def create_state(self, **values: Any) -> dict[str, Any]:
        self.events.append("create_state")
        self.states.append(dict(values))
        return dict(values)


def _scope(*, principal_type: str = "operator") -> ScopeContext:
    return ScopeContext(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        actor_id="operator-a",
        principal_type=principal_type,
        allowed_project_ids=frozenset({"project-a"}),
    )


def _service(
    repository: _Repository,
    *,
    enabled: bool = True,
    registration_error: bool = False,
    callback_registration_ready: bool = False,
    descriptor: dict[str, Any] | None = None,
) -> OAuthAuthorizationService:
    from core.application.extensions.oauth_contract import (
        compile_oauth_providers,
        provider_security_fingerprint,
    )

    compiled_descriptor = descriptor or _descriptor()
    repository.configuration["provider_security_fingerprint"] = provider_security_fingerprint(
        compile_oauth_providers(compiled_descriptor)[0]
    )
    if repository.connection and not repository.connection.get(
        "oauth_provider_security_fingerprint"
    ):
        repository.connection["oauth_provider_security_fingerprint"] = repository.configuration[
            "provider_security_fingerprint"
        ]
    install = {
        "extension_id": "fixture.extension-oauth",
        "release_id": "release-a",
        "enabled": enabled,
        "compiled_manifest": compiled_descriptor,
    }

    def register_callback(**values: Any) -> str:
        repository.events.append("register_callback")
        if registration_error:
            raise OAuthCallbackRegistrationPreconditionError("must-not-escape")
        assert values["project_id"] == "project-a"
        return (
            "https://app.example.test/webhook/extensions/fixture.extension-oauth/"
            "oauth_authorization/opaque-registration-token"
        )

    return OAuthAuthorizationService(
        repository=repository,
        install_loader=lambda _install_id, _account_id: dict(install),
        register_callback=register_callback,
        secret_present=lambda **values: values["ref_key"].endswith(":client_secret"),
        encrypt_verifier=lambda value: f"encrypted:{hashlib.sha256(value.encode()).hexdigest()}",
        transaction_factory=nullcontext,
        callback_registration_ready=lambda **_values: callback_registration_ready,
        state_token_factory=lambda: "opaque-state-token-that-is-long-enough-123456",
        verifier_factory=lambda: "v" * 64,
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )


def test_provider_setup_status_is_read_only_and_role_neutral() -> None:
    repository = _Repository()

    result = _service(
        repository,
        callback_registration_ready=True,
    ).provider_setup_status(_scope(principal_type="viewer"), "install-a", "example")

    assert result.model_dump() == {"callback_registration_ready": True}
    assert repository.events == []


def _command(**overrides: Any) -> OAuthAuthorizationStart:
    values: dict[str, Any] = {
        "configuration_id": "11111111-1111-4111-8111-111111111111",
        "connection_type": "example_mail",
        "intent_id": "connect",
        "display_name": "Primary mailbox",
        "return_page_id": "connections",
        "intent_inputs": {"email_hint": "person@example.test"},
    }
    values.update(overrides)
    return OAuthAuthorizationStart(**values)


def test_start_builds_only_declared_authorization_parameters_and_hashes_state() -> None:
    repository = _Repository()
    result = _service(repository).start_authorization(_scope(), "install-a", "example", _command())

    assert result.redirect_uri.endswith(
        "/webhook/extensions/fixture.extension-oauth/oauth_authorization/opaque-registration-token"
    )
    query = parse_qs(urlsplit(result.authorization_url).query)
    assert query == {
        "access_type": ["offline"],
        "client_id": ["declared-client"],
        "code_challenge": ["w1TpKUdYE9hUAcNSeeSRFioHDxfUxuHho_JHAfZ_vDM"],
        "code_challenge_method": ["S256"],
        "login_hint": ["person@example.test"],
        "prompt": ["consent select_account"],
        "redirect_uri": [result.redirect_uri],
        "response_type": ["code"],
        "scope": ["mail.read mail.write"],
        "state": ["opaque-state-token-that-is-long-enough-123456"],
    }
    assert repository.events == [
        "lock_configuration",
        "register_callback",
        "create_state",
    ]
    state = repository.states[0]
    assert (
        state["state_hash"]
        == hashlib.sha256(b"opaque-state-token-that-is-long-enough-123456").hexdigest()
    )
    assert state["pkce_verifier_encrypted"].startswith("encrypted:")
    assert state["intent_payload"]["return_page_id"] == "connections"
    assert "opaque-state-token-that-is-long-enough-123456" not in repr(state)
    assert "v" * 64 not in repr(state)
    assert set(result.model_dump()) == {"authorization_url", "expires_at", "redirect_uri"}


def test_start_reads_connection_types_from_compiled_descriptor_projection() -> None:
    repository = _Repository()
    descriptor = _descriptor()
    runtime_contract = descriptor["runtime"]["extension_contract_v2"]
    descriptor["connection_types"] = runtime_contract["connection_types"]
    runtime_contract["connection_types"] = "contracts/connection_types.yaml"

    result = _service(repository, descriptor=descriptor).start_authorization(
        _scope(), "install-a", "example", _command()
    )

    assert parse_qs(urlsplit(result.authorization_url).query)["scope"] == ["mail.read mail.write"]
    assert repository.events == [
        "lock_configuration",
        "register_callback",
        "create_state",
    ]


@pytest.mark.parametrize(
    ("intent_inputs", "code", "field"),
    [
        ({"redirect_uri": "https://evil.test"}, "unknown_oauth_intent_input", "redirect_uri"),
        ({"email_hint": "not-an-email"}, "invalid_oauth_intent_input_value", "email_hint"),
    ],
)
def test_start_rejects_undeclared_or_invalid_inputs_before_side_effects(
    intent_inputs: dict[str, str], code: str, field: str
) -> None:
    repository = _Repository()
    _raises_callable_391_1 = _service(repository).start_authorization
    _raises_input_391_2 = _scope()
    _raises_input_391_3 = _command(intent_inputs=intent_inputs)
    with pytest.raises(ServiceValidationError) as exc_info:
        _raises_callable_391_1(_raises_input_391_2, "install-a", "example", _raises_input_391_3)
    assert exc_info.value.payload == {"error": code, "field": field}
    assert repository.events == ["lock_configuration"]


def test_start_rejects_disabled_install_before_registration_or_state() -> None:
    repository = _Repository()
    _raises_callable_401_1 = _service(repository, enabled=False).start_authorization
    _raises_input_401_2 = _scope()
    _raises_input_401_3 = _command()
    with pytest.raises(ServiceNotFoundError):
        _raises_callable_401_1(_raises_input_401_2, "install-a", "example", _raises_input_401_3)
    assert repository.events == []


def test_reconnect_locks_exact_connection_scope_before_callback_registration() -> None:
    repository = _Repository()
    _service(repository).start_authorization(
        _scope(),
        "install-a",
        "example",
        _command(existing_connection_id="connection-a"),
    )
    assert repository.events == [
        "lock_configuration",
        "lock_connection",
        "register_callback",
        "create_state",
    ]
    assert repository.states[0]["existing_connection_id"] == "connection-a"
    assert repository.states[0]["intent_payload"] == {
        "authorization_parameters": {"login_hint": "person@example.test"},
        "connection_credential_revision": 7,
        "return_page_id": "connections",
    }


def test_bound_reconnect_without_input_uses_saved_verified_subject() -> None:
    repository = _Repository()
    repository.connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "disconnected",
            "subject": "acme.identity.example.test",
        }
    }

    result = _service(repository, descriptor=_bound_descriptor()).start_authorization(
        _scope(),
        "install-a",
        "example",
        _command(intent_inputs={}, existing_connection_id="connection-a"),
    )

    assert urlsplit(result.authorization_url).netloc == "acme.identity.example.test"
    assert repository.events == [
        "lock_configuration",
        "lock_connection",
        "register_callback",
        "create_state",
    ]
    assert repository.states[0]["intent_payload"] == {
        "authorization_parameters": {},
        "authorization_origin": "acme.identity.example.test",
        "connection_credential_revision": 7,
        "return_page_id": "connections",
    }


def test_bound_reconnect_restores_the_saved_subject_as_a_declared_label() -> None:
    repository = _Repository()
    saved_subject = f"{'a' * 60}.identity.example.test"
    repository.connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "disconnected",
            "subject": saved_subject,
        }
    }

    result = _service(repository, descriptor=_bound_descriptor()).start_authorization(
        _scope(),
        "install-a",
        "example",
        _command(intent_inputs={}, existing_connection_id="connection-a"),
    )

    assert urlsplit(result.authorization_url).netloc == saved_subject
    assert repository.states[0]["intent_payload"]["authorization_origin"] == saved_subject


def test_bound_reconnect_accepts_only_a_normalized_match_for_saved_subject() -> None:
    repository = _Repository()
    repository.connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "connected",
            "subject": "acme.identity.example.test",
        }
    }

    result = _service(repository, descriptor=_bound_descriptor()).start_authorization(
        _scope(),
        "install-a",
        "example",
        _command(
            intent_inputs={"tenant_domain": "ACME"},
            existing_connection_id="connection-a",
        ),
    )

    assert urlsplit(result.authorization_url).netloc == "acme.identity.example.test"
    assert repository.states[0]["intent_payload"]["authorization_origin"] == (
        "acme.identity.example.test"
    )


def test_bound_reconnect_rejects_a_different_subject_before_side_effects() -> None:
    repository = _Repository()
    repository.connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "connected",
            "subject": "acme.identity.example.test",
        }
    }

    _raises_callable_516_1 = _service(
        repository, descriptor=_bound_descriptor()
    ).start_authorization
    _raises_input_516_2 = _scope()
    _raises_input_516_3 = _command(
        intent_inputs={"tenant_domain": "other"}, existing_connection_id="connection-a"
    )
    with pytest.raises(ServiceValidationError) as exc_info:
        _raises_callable_516_1(_raises_input_516_2, "install-a", "example", _raises_input_516_3)

    assert exc_info.value.payload == {
        "error": "oauth_reconnect_subject_mismatch",
        "field": "tenant_domain",
    }
    assert repository.events == ["lock_configuration", "lock_connection"]
    assert repository.states == []


@pytest.mark.parametrize(
    "saved_subject",
    [None, 123, "", "not a valid host", "ACME", "acme"],
)
def test_bound_reconnect_rejects_missing_or_invalid_saved_subject(
    saved_subject: Any,
) -> None:
    repository = _Repository()
    repository.connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "disconnected",
            "subject": saved_subject,
        }
    }

    _raises_callable_550_1 = _service(
        repository, descriptor=_bound_descriptor()
    ).start_authorization
    _raises_input_550_2 = _scope()
    _raises_input_550_3 = _command(intent_inputs={}, existing_connection_id="connection-a")
    with pytest.raises(ServicePreconditionError) as exc_info:
        _raises_callable_550_1(_raises_input_550_2, "install-a", "example", _raises_input_550_3)

    assert exc_info.value.payload == {"error": "oauth_reconnect_subject_unavailable"}
    assert repository.events == ["lock_configuration", "lock_connection"]
    assert repository.states == []


def test_new_bound_connection_without_origin_input_stays_rejected() -> None:
    repository = _Repository()

    _raises_callable_566_1 = _service(
        repository, descriptor=_bound_descriptor()
    ).start_authorization
    _raises_input_566_2 = _scope()
    _raises_input_566_3 = _command(intent_inputs={})
    with pytest.raises(ServiceValidationError) as exc_info:
        _raises_callable_566_1(_raises_input_566_2, "install-a", "example", _raises_input_566_3)

    assert exc_info.value.payload == {
        "error": "invalid_oauth_intent_input_value",
        "field": "tenant_domain",
    }
    assert repository.events == ["lock_configuration"]
    assert repository.states == []


@pytest.mark.parametrize(
    "return_page_id", ["unknown", "oauth_help", "../connections", "Connections", ""]
)
def test_start_rejects_undeclared_or_malformed_return_page_before_side_effects(
    return_page_id: str,
) -> None:
    repository = _Repository()

    def start_authorization() -> None:
        _service(repository).start_authorization(
            _scope(),
            "install-a",
            "example",
            _command(return_page_id=return_page_id),
        )

    with pytest.raises((ServiceValidationError, ValidationError)):
        start_authorization()

    assert repository.states == []


def test_release_upgrade_authorization_required_connection_can_reconnect() -> None:
    repository = _Repository()
    repository.connection["connection_config"] = {
        "oauth": {"lifecycle_status": "authorization_required"}
    }

    _service(repository).start_authorization(
        _scope(),
        "install-a",
        "example",
        _command(existing_connection_id="connection-a"),
    )

    assert repository.events == [
        "lock_configuration",
        "lock_connection",
        "register_callback",
        "create_state",
    ]
    assert repository.states[0]["existing_connection_id"] == "connection-a"


@pytest.mark.parametrize(
    ("enabled", "lifecycle_status"),
    [(False, "disconnecting"), (True, "disconnecting")],
)
def test_reconnect_rejects_connection_being_disconnected_before_state_creation(
    enabled: bool, lifecycle_status: str
) -> None:
    repository = _Repository()
    repository.connection["enabled"] = enabled
    repository.connection["connection_config"] = {"oauth": {"lifecycle_status": lifecycle_status}}

    _raises_callable_634_1 = _service(repository).start_authorization
    _raises_input_634_2 = _scope()
    _raises_input_634_3 = _command(existing_connection_id="connection-a")
    with pytest.raises(ServicePreconditionError) as exc_info:
        _raises_callable_634_1(_raises_input_634_2, "install-a", "example", _raises_input_634_3)

    assert exc_info.value.payload == {"error": "oauth_connection_disconnect_in_progress"}
    assert repository.events == ["lock_configuration", "lock_connection"]
    assert repository.states == []


def test_reconnect_conceals_missing_or_foreign_connection() -> None:
    repository = _Repository()
    repository.connection = {}
    _raises_callable_650_1 = _service(repository).start_authorization
    _raises_input_650_2 = _scope()
    _raises_input_650_3 = _command(existing_connection_id="foreign")
    with pytest.raises(ServiceNotFoundError, match="Connection not found"):
        _raises_callable_650_1(_raises_input_650_2, "install-a", "example", _raises_input_650_3)
    assert repository.events == ["lock_configuration", "lock_connection"]
    assert repository.states == []


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("connection_type", "example_files"),
        ("oauth_configuration_id", "22222222-2222-4222-8222-222222222222"),
        ("oauth_provider_id", "other"),
        ("oauth_extension_release_id", "release-other"),
        ("oauth_provider_security_fingerprint", "fingerprint-other"),
        ("extension_install_id", "install-other"),
        ("extension_id", "fixture.other"),
        ("project_id", "project-other"),
        ("account_id", "account-other"),
        ("tenant_id", "tenant-other"),
    ],
)
def test_reconnect_conceals_every_foreign_connection_dimension(field: str, foreign: str) -> None:
    repository = _Repository()
    repository.connection[field] = foreign
    _raises_callable_679_1 = _service(repository).start_authorization
    _raises_input_679_2 = _scope()
    _raises_input_679_3 = _command(existing_connection_id="connection-a")
    with pytest.raises(ServiceNotFoundError, match="Connection not found"):
        _raises_callable_679_1(_raises_input_679_2, "install-a", "example", _raises_input_679_3)
    assert repository.events == ["lock_configuration", "lock_connection"]


@pytest.mark.parametrize("status", ["incomplete", "needs_attention"])
def test_start_rejects_configuration_that_is_not_ready(status: str) -> None:
    repository = _Repository()
    repository.configuration["status"] = status
    _raises_callable_693_1 = _service(repository).start_authorization
    _raises_input_693_2 = _scope()
    _raises_input_693_3 = _command()
    with pytest.raises(ServicePreconditionError) as exc_info:
        _raises_callable_693_1(_raises_input_693_2, "install-a", "example", _raises_input_693_3)
    assert getattr(exc_info.value, "payload", {}) == {"error": "oauth_configuration_not_ready"}
    assert repository.events == ["lock_configuration"]


def test_start_command_rejects_non_uuid_configuration_id() -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationStart(
            configuration_id="not-a-uuid-not-a-uuid-not-a-uuid-xx",
            connection_type="example_mail",
            intent_id="connect",
            display_name="Mailbox",
            return_page_id="connections",
        )


def test_start_command_trims_intent_input_values_at_the_api_boundary() -> None:
    command = _command(intent_inputs={"email_hint": "  person@example.test  "})

    assert command.intent_inputs == {"email_hint": "person@example.test"}


def test_start_rechecks_required_secret_presence_before_side_effects() -> None:
    repository = _Repository()
    repository.configuration["secret_refs"] = {}
    _raises_callable_719_1 = _service(repository).start_authorization
    _raises_input_719_2 = _scope()
    _raises_input_719_3 = _command()
    with pytest.raises(ServicePreconditionError) as exc_info:
        _raises_callable_719_1(_raises_input_719_2, "install-a", "example", _raises_input_719_3)
    assert getattr(exc_info.value, "payload", {}) == {"error": "oauth_configuration_not_ready"}
    assert repository.events == ["lock_configuration"]


def test_corrupt_or_disabled_callback_registration_never_creates_state() -> None:
    repository = _Repository()
    _raises_callable_727_1 = _service(repository, registration_error=True).start_authorization
    _raises_input_727_2 = _scope()
    _raises_input_727_3 = _command()
    with pytest.raises(ServicePreconditionError) as exc_info:
        _raises_callable_727_1(_raises_input_727_2, "install-a", "example", _raises_input_727_3)
    assert exc_info.value.payload == {"error": "oauth_callback_registration_unavailable"}
    assert "must-not-escape" not in str(exc_info.value.payload)
    assert repository.events == ["lock_configuration", "register_callback"]
    assert repository.states == []


def test_start_rejects_flat_or_foreign_secret_refs_before_side_effects() -> None:
    for secret_refs in (
        {"client_secret": "extension_oauth_configuration:foreign:client_secret"},
        {"refs": {"client_secret": "extension_oauth_configuration:foreign:client_secret"}},
    ):
        repository = _Repository()
        repository.configuration["secret_refs"] = secret_refs
        _raises_callable_744_1 = _service(repository).start_authorization
        _raises_input_744_2 = _scope()
        _raises_input_744_3 = _command()
        with pytest.raises(ServicePreconditionError) as exc_info:
            _raises_callable_744_1(_raises_input_744_2, "install-a", "example", _raises_input_744_3)
        assert exc_info.value.payload == {"error": "oauth_configuration_not_ready"}
        assert repository.events == ["lock_configuration"]
