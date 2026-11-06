"""Connections started from an imported refresh token (ADR 0058).

Every provider here is a neutral fixture: Core must not know any real one.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from core.application.extensions.browser_manifest_projection import project_browser_manifest
from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.application.extensions.oauth_authorization import (
    OAuthAuthorizationService,
    OAuthAuthorizationStart,
)
from core.application.extensions.oauth_callback_connection_checks import (
    _current_install_provider,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_grant_commit import OAuthGrant
from core.application.extensions.oauth_refresh_token_import import (
    OAuthRefreshTokenImport,
    OAuthRefreshTokenImportService,
)
from core.application.extensions.oauth_runtime import OAuthRuntimeTokenService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.api.extension_oauth import parse_refresh_token_import
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthProviderOperation,
)

CONFIGURATION_ID = "33333333-3333-4333-8333-333333333333"
PASTED = "pasted-refresh-token-canary"
CLIENT_SECRET = "client-secret-canary"  # pragma: allowlist secret


def _credential_fields() -> list[dict[str, Any]]:
    return [
        {
            "field_id": "client_id",
            "value_type": "text",
            "required": True,
            "secret": False,
            "change_policy": "reauthorization_required",
            "label": "Client ID",
            "help_text": "Enter the application client ID.",
            "validation": {"min_length": 1, "max_length": 512},
        },
        {
            "field_id": "client_secret",
            "value_type": "secret",
            "required": True,
            "secret": True,
            "change_policy": "runtime_revalidate",
            "label": "Client secret",
            "help_text": "Enter the application client secret.",
            "validation": {"min_length": 1, "max_length": 4096},
        },
    ]


def _import_provider() -> dict[str, Any]:
    return {
        "provider_id": "console",
        "display_name": "Console Identity",
        "description": "Connect with a refresh token issued in the provider console.",
        "setup_page_id": "oauth_setup",
        "help_page_id": "oauth_help",
        "error_help_anchors": {"invalid_grant": "replace-token"},
        "flow": {"type": "refresh_token_import"},
        "credential_fields": _credential_fields(),
        "token_endpoint": {
            "url": "https://identity.example.test/token",
            "client_auth_method": "client_secret_post",
        },
        "revocation_endpoint": {"url": "https://identity.example.test/revoke"},
        "post_connect_action_id": "verify_identity",
    }


def _config_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["account_ref", "region"],
        "properties": {
            "account_ref": {"type": "string", "minLength": 1, "maxLength": 64},
            "region": {"type": "string", "enum": ["north", "south"]},
        },
    }


def _descriptor(
    provider: dict[str, Any] | None = None, *, required_scopes: list[str] | None = None
) -> dict[str, Any]:
    return {
        "extension_id": "fixture.oauth-import",
        "action_definitions": [
            {
                "action_id": "verify_identity",
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "workflow_visible": False,
                "parameters": [],
            }
        ],
        "ui_manifest": {
            "version": "1",
            "pages": [
                {"page_id": "oauth_setup", "title": "Setup", "components": []},
                {
                    "page_id": "oauth_help",
                    "title": "Help",
                    "components": [{"type": "markdown", "anchor_id": "replace-token"}],
                },
            ],
        },
        "runtime": {
            "compatibility": {"platform_min": "1.6.0"},
            "extension_contract_v2": {
                "oauth_providers": [provider or _import_provider()],
                "connection_types": [
                    {
                        "connection_type_id": "console_account",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "console",
                            "scopes": {"required": list(required_scopes or []), "optional": []},
                        },
                        "config_schema": _config_schema(),
                    }
                ],
            },
        },
    }


def _error_codes(result: dict[str, Any]) -> set[str]:
    return {str(row.get("code") or "") for row in result["errors"]}


# --- contract ---------------------------------------------------------------


def test_minimal_import_provider_compiles_without_redirect_keys() -> None:
    result = validate_extension_contract_v2(_descriptor())

    assert result["valid"] is True, result["errors"]
    provider = compile_oauth_providers(_descriptor())[0]
    assert provider.is_refresh_token_import
    assert provider.flow_dict() == {"type": "refresh_token_import"}
    assert provider.callback_id == ""
    assert provider.authorization_intents == ()
    rendered = result["normalized_contract"]["oauth_providers"][0]
    assert rendered["flow"] == {"type": "refresh_token_import"}
    for redirect_only in ("authorization_endpoint", "callback_id", "authorization_intents"):
        assert redirect_only not in rendered


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("authorization_endpoint", {"url": "https://identity.example.test/authorize"}),
        ("callback_id", "oauth_authorization"),
        ("authorization_intents", [{"intent_id": "connect", "static_parameters": {}}]),
        (
            "origin_binding",
            {
                "binding_id": "tenant",
                "input_id": "tenant",
                "host_suffix": ".example.test",
                "label": {"charset": "lowercase_alnum", "min_length": 1, "max_length": 10},
            },
        ),
        (
            "callback_verification",
            {
                "signature_parameter": "signature",
                "algorithm": "hmac_sha256",
                "digest": "hex",
                "message": "sorted_query_excluding_signature",
                "secret_source": "client_secret",  # pragma: allowlist secret
                "subject_parameter": "tenant",
                "subject_binding": "tenant",
            },
        ),
    ],
)
def test_import_provider_refuses_every_redirect_only_key(key: str, value: Any) -> None:
    provider = _import_provider()
    provider[key] = value

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert {
        (row["path"], row["code"])
        for row in result["errors"]
        if row["code"] == "oauth_key_not_allowed_for_flow"
    } == {(f"extension_contract_v2.oauth_providers[0].{key}", "oauth_key_not_allowed_for_flow")}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("include_grant_type", False),
        ("include_redirect_uri", False),
        ("static_parameters", {"audience": "api"}),
    ],
)
def test_import_provider_refuses_the_request_half_of_code_exchange(key: str, value: Any) -> None:
    provider = _import_provider()
    provider["code_exchange"] = {key: value, "implicit_token_type": "bearer"}

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert {
        row["path"] for row in result["errors"] if row["code"] == "oauth_key_not_allowed_for_flow"
    } == {f"extension_contract_v2.oauth_providers[0].code_exchange.{key}"}


def test_import_provider_declares_how_its_token_response_reads() -> None:
    """A provider is not narrower for being imported: the response profile and the
    scope separator that refresh relies on are declarable, and both are part of
    what a grant depends on."""
    provider = _import_provider()
    provider["scope_separator"] = "comma"
    provider["code_exchange"] = {
        "implicit_token_type": "bearer",
        "required_response_fields": ["access_token", "expires_in"],
    }

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is True, result["errors"]
    compiled = compile_oauth_providers(_descriptor(provider))[0]
    assert compiled.scope_separator == "comma"
    assert compiled.code_exchange.implicit_token_type == "bearer"
    assert compiled.code_exchange.required_response_fields == ("access_token", "expires_in")
    baseline = compile_oauth_providers(_descriptor())[0]
    assert compiled.to_dict()["security_fingerprint"] != baseline.to_dict()["security_fingerprint"]


def test_import_exchange_reads_the_response_through_the_declared_profile() -> None:
    provider = _import_provider()
    provider["scope_separator"] = "comma"
    provider["code_exchange"] = {"implicit_token_type": "bearer"}
    descriptor = _descriptor(provider, required_scopes=["read"])
    repository = _Repository(descriptor)
    service, _store, calls = _service(
        repository,
        descriptor,
        refresh=lambda **_values: {
            "access_token": "access-token-canary",
            "expires_in": 3600,
            "scope": "read,write",
        },
    )

    service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert calls[0]["implicit_token_type"] == "bearer"
    assert repository.created[0]["oauth_granted_scopes"] == ["read", "write"]


def test_import_provider_refuses_pkce_and_unique_subject() -> None:
    provider = _import_provider()
    provider["flow"] = {"type": "refresh_token_import", "pkce": "s256"}
    provider["unique_subject_per_configuration"] = True

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    paths = {row["path"]: row["code"] for row in result["errors"]}
    assert paths["extension_contract_v2.oauth_providers[0].flow.pkce"] == "invalid_oauth_flow"
    assert (
        paths["extension_contract_v2.oauth_providers[0].unique_subject_per_configuration"]
        == "invalid_oauth_unique_subject"
    )


def test_import_provider_accepts_explicit_false_unique_subject() -> None:
    provider = _import_provider()
    provider["unique_subject_per_configuration"] = False

    assert validate_extension_contract_v2(_descriptor(provider))["valid"] is True


def test_unknown_flow_type_names_both_closed_values() -> None:
    provider = _import_provider()
    provider["flow"] = {"type": "client_credentials"}

    result = validate_extension_contract_v2(_descriptor(provider))

    assert "invalid_oauth_flow" in _error_codes(result)


def test_empty_required_scopes_are_allowed_only_for_an_import_provider() -> None:
    assert validate_extension_contract_v2(_descriptor())["valid"] is True

    redirect = _manifest_fixture()
    redirect["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"]["scopes"] = {
        "required": [],
        "optional": [],
    }
    redirect["ui_manifest"] = {"version": "1", "pages": []}
    result = validate_extension_contract_v2(redirect)
    assert "missing_connection_auth_oauth_required_scope" in _error_codes(result)


def _manifest_fixture() -> dict[str, Any]:
    manifest = yaml.safe_load(
        Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest["action_definitions"] = [
        {
            "action_id": "verify_identity",
            "handler": {"mode": "extension"},
            "mutates_platform": False,
            "workflow_visible": False,
            "parameters": [],
        }
    ]
    return manifest


#: Computed on public main 5764b4d, before the refresh-token flow existed. An
#: authorization-code provider must compile to exactly these bytes, or every
#: existing configuration would be sent through release reconciliation.
_BASELINE_FIXTURE_FINGERPRINT = (
    "9a2fb10e0db788368bf0ce32ded169a23de68ad40ddd4141a81edab0e804e839"  # pragma: allowlist secret
)
_BASELINE_FIXTURE_RENDERED_SHA256 = (
    "7145db6dce6cf9a94c02d567111129bc46d6558473857313e698e27dd219af9f"  # pragma: allowlist secret
)


def test_authorization_code_provider_compiles_byte_identical_to_the_baseline() -> None:
    provider = compile_oauth_providers(_manifest_fixture())[0]

    assert provider.flow_type == "authorization_code"
    assert provider.flow_dict() == {"type": "authorization_code", "pkce": "s256"}
    assert provider_security_fingerprint(provider) == _BASELINE_FIXTURE_FINGERPRINT
    rendered = json.dumps(provider.to_dict(), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(rendered.encode()).hexdigest() == _BASELINE_FIXTURE_RENDERED_SHA256


def test_browser_projection_exposes_flow_and_connection_schema_but_no_secret() -> None:
    provider = compile_oauth_providers(_descriptor())[0]
    manifest = {
        "oauth_providers": [provider.to_dict()],
        "connection_types": _descriptor()["runtime"]["extension_contract_v2"]["connection_types"],
    }

    projected = project_browser_manifest(manifest)

    assert projected["oauth_providers"][0]["flow"] == {"type": "refresh_token_import"}
    assert projected["oauth_providers"][0]["authorization_intents"] == []
    assert projected["connection_types"][0]["config_schema"] == _config_schema()
    assert "security_fingerprint" not in projected["oauth_providers"][0]


def test_redirect_entry_points_refuse_an_import_provider() -> None:
    service = OAuthAuthorizationService(
        repository=None,  # type: ignore[arg-type]
        install_loader=lambda _install, _account: {
            "extension_id": "fixture.oauth-import",
            "release_id": "release-a",
            "enabled": True,
            "compiled_manifest": _descriptor(),
        },
        register_callback=lambda **_values: pytest.fail("no callback for an import provider"),
        secret_present=lambda **_values: True,
        encrypt_verifier=lambda value: value,
        transaction_factory=contextmanager(lambda: (yield)),
    )
    scope = _scope()
    with pytest.raises(ServiceValidationError) as started:
        service.start_authorization(
            scope,
            "install-a",
            "console",
            OAuthAuthorizationStart(
                configuration_id=CONFIGURATION_ID,
                connection_type="console_account",
                intent_id="connect",
                display_name="Account",
                return_page_id="oauth_setup",
            ),
        )
    assert started.value.payload == {"error": "unsupported_oauth_flow"}
    with pytest.raises(ServiceValidationError):
        service.ensure_redirect_uri(scope, "install-a", "console")
    status = service.provider_setup_status(scope, "install-a", "console")
    assert status.callback_registration_ready is True


def test_callback_state_and_import_state_cannot_stand_in_for_each_other() -> None:
    provider = compile_oauth_providers(_descriptor())[0]
    install = {
        "extension_id": "fixture.oauth-import",
        "release_id": "release-a",
        "enabled": True,
        "compiled_manifest": _descriptor(),
    }
    state = {
        "extension_install_id": "install-a",
        "account_id": "account-a",
        "extension_id": "fixture.oauth-import",
        "extension_release_id": "release-a",
        "provider_id": "console",
        "callback_id": "",
        "provider_security_fingerprint": provider_security_fingerprint(provider),
    }

    def loader(_install: str, _account: str) -> dict[str, Any]:
        return copy.deepcopy(install)

    # A stored callback state names no flow and is authorization-code.
    assert _current_install_provider(state, install_loader=loader) is None
    assert (
        _current_install_provider(
            {**state, "flow_type": "refresh_token_import"}, install_loader=loader
        )
        is not None
    )


# --- service ----------------------------------------------------------------


def _scope(principal_type: str = "operator") -> ScopeContext:
    return ScopeContext(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        principal_type=principal_type,
    )


class _Repository:
    def __init__(self, descriptor: dict[str, Any]) -> None:
        provider = compile_oauth_providers(descriptor)[0]
        self.events: list[str] = []
        self.configuration = {
            "configuration_id": CONFIGURATION_ID,
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.oauth-import",
            "extension_release_id": "release-a",
            "provider_id": "console",
            "configuration_revision": 4,
            "authorization_revision": 2,
            "status": "ready_for_authorization",
            "non_secret_config": {"client_id": "client-id"},
            "secret_refs": {
                "refs": {
                    "client_secret": f"extension_oauth_configuration:{CONFIGURATION_ID}:client_secret"
                }
            },
            "provider_security_fingerprint": provider_security_fingerprint(provider),
            "provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
        }
        self.connections: dict[str, dict[str, Any]] = {
            "connection-existing": {
                "connection_id": "connection-existing",
                "tenant_id": "tenant-a",
                "account_id": "account-a",
                "project_id": "project-a",
                "extension_install_id": "install-a",
                "extension_id": "fixture.oauth-import",
                "oauth_provider_id": "console",
                "oauth_configuration_id": CONFIGURATION_ID,
                "oauth_extension_release_id": "release-a",
                "oauth_authorization_revision": 2,
                "oauth_provider_security_fingerprint": provider_security_fingerprint(provider),
                "oauth_provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
                "connection_type": "console_account",
                "credential_revision": 5,
                "enabled": True,
                "secret_refs": {
                    "refs": {
                        "_oauth_refresh_token": (
                            "project_connection:connection-existing:_oauth_refresh_token"
                        )
                    }
                },
                "connection_config": {
                    "account_ref": "old-account",
                    "region": "north",
                    "oauth": {"lifecycle_status": "authorization_required"},
                },
            }
        }
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.post_connect_updates: list[dict[str, Any]] = []
        self.invalid_client_marks: list[dict[str, Any]] = []
        self.configuration_reads = 0
        #: Bump the configuration revision once it has been read this many times.
        self.bump_configuration_after_reads = 0

    def get_configuration_for_scope_for_update(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("lock_configuration")
        self.configuration_reads += 1
        if self.bump_configuration_after_reads and (
            self.configuration_reads > self.bump_configuration_after_reads
        ):
            self.configuration["configuration_revision"] = 99
        return copy.deepcopy(self.configuration)

    def mark_configuration_invalid_client(self, **scope: Any) -> dict[str, Any] | None:
        self.events.append("mark_configuration_invalid_client")
        self.invalid_client_marks.append(copy.deepcopy(scope))
        self.configuration["status"] = "needs_attention"
        return copy.deepcopy(self.configuration)

    def lock_oauth_install_for_update(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("lock_install")
        return {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.oauth-import",
            "release_id": "release-a",
            "state": "active",
        }

    def lock_connection(self, **scope: Any) -> dict[str, Any]:
        self.events.append("lock_connection")
        return copy.deepcopy(self.connections.get(str(scope.get("connection_id")), {}))

    def create_oauth_connection(self, **values: Any) -> dict[str, Any]:
        self.events.append("create_connection")
        self.created.append(copy.deepcopy(values))
        self.connections[values["connection_id"]] = {
            **copy.deepcopy(values),
            "credential_revision": 1,
            "enabled": True,
        }
        return copy.deepcopy(values)

    def update_oauth_connection(self, **values: Any) -> dict[str, Any] | None:
        self.events.append("update_connection")
        current = self.connections[values["connection_id"]]
        if values["expected_credential_revision"] != current["credential_revision"]:
            return None
        self.updated.append(copy.deepcopy(values))
        current.update(copy.deepcopy(values))
        current["credential_revision"] += 1
        return copy.deepcopy(current)

    def mark_configuration_verified(self, **_scope: Any) -> bool:
        self.events.append("mark_configuration_verified")
        return True

    def update_oauth_post_connect(self, **values: Any) -> dict[str, Any]:
        self.events.append("update_post_connect")
        self.post_connect_updates.append(copy.deepcopy(values))
        current = self.connections[values["connection_id"]]
        if values["expected_credential_revision"] != current["credential_revision"]:
            return {}
        current["connection_config"] = copy.deepcopy(values["connection_config"])
        current["credential_revision"] += 1
        return copy.deepcopy(current)


class _TokenStore:
    """Behaves like the database store: a missing refresh token keeps the fallback."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.writes: list[dict[str, Any]] = []
        self.stored_refresh_token = ""

    def resolve_refresh_token(self, **_values: Any) -> str:
        self.events.append("resolve_refresh_token")
        return "previously-stored-refresh-token"

    def resolve_access_token(self, **_values: Any) -> str:
        return "unused"

    def write_tokens(self, **values: Any) -> dict[str, Any]:
        self.events.append("write_tokens")
        self.writes.append(copy.deepcopy(values))
        response = values["token_response"]
        self.stored_refresh_token = str(response.get("refresh_token") or "") or str(
            values["existing_refresh_token"]
        )
        connection_id = values["connection_id"]
        return {
            "refs": {
                "_oauth_access_token": f"project_connection:{connection_id}:_oauth_access_token",
                "_oauth_refresh_token": f"project_connection:{connection_id}:_oauth_refresh_token",
            }
        }


def _ok_post_connect(captured: list[dict[str, Any]]):
    def post_connect(**values: Any) -> dict[str, Any]:
        captured.append(copy.deepcopy(values))
        return {
            "status": "succeeded",
            "response": {
                "result": {
                    "subject": "account-subject",
                    "verified_display_name": "Verified account",
                    "safe_metadata": {},
                    "health": "ok",
                }
            },
        }

    return post_connect


def _service(
    repository: _Repository,
    descriptor: dict[str, Any],
    *,
    refresh=None,
    post_connect=None,
) -> tuple[OAuthRefreshTokenImportService, _TokenStore, list[dict[str, Any]]]:
    token_store = _TokenStore(repository.events)
    calls: list[dict[str, Any]] = []

    @contextmanager
    def transaction():
        repository.events.append("tx_begin")
        try:
            yield
        except Exception:
            repository.events.append("tx_rollback")
            raise
        repository.events.append("tx_commit")

    def refresh_access_token(**values: Any) -> dict[str, Any]:
        repository.events.append("exchange")
        calls.append(copy.deepcopy(values))
        if refresh is not None:
            return refresh(**values)
        return {"access_token": "access-token-canary", "token_type": "bearer", "expires_in": 3600}

    service = OAuthRefreshTokenImportService(
        repository=repository,  # type: ignore[arg-type]
        token_store=token_store,
        install_loader=lambda _install, _account: {
            "extension_id": "fixture.oauth-import",
            "release_id": "release-a",
            "enabled": True,
            "compiled_manifest": copy.deepcopy(descriptor),
        },
        secret_present=lambda **_values: True,
        configuration_secret_value=lambda **_values: CLIENT_SECRET,
        refresh_access_token=refresh_access_token,
        transaction_factory=transaction,
        invoke_post_connect=post_connect,
        connection_id_factory=lambda: "connection-new",
    )
    return service, token_store, calls


def _command(**overrides: Any) -> OAuthRefreshTokenImport:
    values: dict[str, Any] = {
        "configuration_id": CONFIGURATION_ID,
        "connection_type": "console_account",
        "display_name": "Primary account",
        "refresh_token": f"  {PASTED}\n",
        "connection_config": {"account_ref": "acct-1", "region": "south"},
    }
    values.update(overrides)
    return OAuthRefreshTokenImport.model_validate(values)


def test_import_happy_path_commits_through_the_shared_grant_path() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    captured: list[dict[str, Any]] = []
    service, token_store, calls = _service(
        repository, descriptor, post_connect=_ok_post_connect(captured)
    )

    result = service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert result.outcome == "connected"
    assert result.connection_id == "connection-new"
    # One exchange, with the configuration's client credentials and the pasted
    # token trimmed, after every check and before any write.
    assert len(calls) == 1
    assert calls[0]["refresh_token"] == PASTED
    assert calls[0]["client_id"] == "client-id"
    assert calls[0]["client_secret"] == CLIENT_SECRET
    assert calls[0]["endpoint"].url == "https://identity.example.test/token"
    assert repository.events.index("exchange") < repository.events.index("write_tokens")
    assert "write_tokens" not in repository.events[: repository.events.index("exchange")]
    created = repository.created[0]
    assert created["oauth_requested_scopes"] == []
    assert created["oauth_granted_scopes"] == []
    assert created["connection_config"]["account_ref"] == "acct-1"
    assert created["connection_config"]["region"] == "south"
    assert created["connection_config"]["oauth"] == {
        "lifecycle_status": "post_connect_pending",
        "token_type": "Bearer",
    }
    assert created["display_name"] == "Primary account"
    # The provider did not rotate it, so the pasted token is what is stored.
    assert token_store.writes[0]["existing_refresh_token"] == PASTED
    assert token_store.stored_refresh_token == PASTED
    # The extension gets the access token and the settings, never the refresh
    # token or the client secret.
    target = captured[0]["target"]
    assert target["connection"]["secrets"] == {"access_token": "access-token-canary"}
    assert target["connection"]["config"] == {
        "account_ref": "acct-1",
        "region": "south",
        "oauth": {"lifecycle_status": "post_connect_pending", "token_type": "Bearer"},
    }
    assert captured[0]["context"]["granted_scopes"] == []
    serialized = repr(captured)
    assert PASTED not in serialized
    assert CLIENT_SECRET not in serialized
    assert repository.post_connect_updates[0]["connection_config"]["oauth"]["subject"] == (
        "account-subject"
    )
    assert PASTED not in repr(result.model_dump())


def test_rotating_response_stores_the_new_refresh_token() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, token_store, _calls = _service(
        repository,
        descriptor,
        refresh=lambda **_values: {
            "access_token": "access-token-canary",
            "refresh_token": "rotated-refresh-token",
            "token_type": "bearer",
            "expires_in": 3600,
        },
    )

    result = service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert result.outcome == "connected"
    assert token_store.stored_refresh_token == "rotated-refresh-token"


@pytest.mark.parametrize(
    ("code", "expected_type", "expected_error"),
    [
        (OAuthErrorCode.INVALID_GRANT, ServiceValidationError, "oauth_refresh_token_rejected"),
        (OAuthErrorCode.INVALID_CLIENT, ServicePreconditionError, "oauth_invalid_client"),
        (
            OAuthErrorCode.PROVIDER_UNAVAILABLE,
            ServiceUnavailableError,
            "oauth_provider_unavailable",
        ),
    ],
)
def test_refused_token_writes_nothing_and_never_echoes_it(
    code: OAuthErrorCode, expected_type: type[Exception], expected_error: str
) -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)

    def refuse(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(code, OAuthProviderOperation.REFRESH)

    service, token_store, _calls = _service(repository, descriptor, refresh=refuse)

    with pytest.raises(expected_type) as raised:
        service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert raised.value.payload["error"] == expected_error  # type: ignore[attr-defined]
    assert PASTED not in repr(raised.value.payload)  # type: ignore[attr-defined]
    assert PASTED not in str(raised.value)
    assert token_store.writes == []
    assert repository.created == []
    assert repository.updated == []
    assert "mark_configuration_verified" not in repository.events


@pytest.mark.parametrize(
    "token", ["", "   ", "has space inside", "tab\tinside", "é-non-ascii", "x" * 8193]
)
def test_unusable_token_is_refused_before_any_lookup(token: str) -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, _store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceValidationError) as raised:
        service.import_refresh_token(
            _scope(), "install-a", "console", _command(refresh_token=token)
        )

    assert raised.value.payload == {
        "error": "invalid_oauth_refresh_token",
        "field": "refresh_token",
    }
    assert calls == []
    assert repository.events == []


def test_viewer_is_refused_before_anything_happens() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, _store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceForbiddenError):
        service.import_refresh_token(_scope("viewer"), "install-a", "console", _command())

    assert calls == []
    assert repository.events == []


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (
            {"account_ref": "acct-1", "region": "east"},
            {"error": "invalid_connection_config", "field": "connection_config.region"},
        ),
        (
            {"account_ref": "acct-1"},
            {"error": "invalid_connection_config", "field": "connection_config.region"},
        ),
        (
            {"account_ref": "acct-1", "region": "south", "oauth": {"subject": "forged"}},
            {"error": "oauth_connection_config_reserved", "field": "connection_config.oauth"},
        ),
    ],
)
def test_settings_are_validated_against_the_connection_type_schema(
    settings: dict[str, Any], expected: dict[str, str]
) -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, token_store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceValidationError) as raised:
        service.import_refresh_token(
            _scope(), "install-a", "console", _command(connection_config=settings)
        )

    assert raised.value.payload == expected
    assert calls == []
    assert token_store.writes == []


def test_replacement_keeps_the_connection_and_replaces_its_settings() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, token_store, _calls = _service(repository, descriptor)

    result = service.import_refresh_token(
        _scope(),
        "install-a",
        "console",
        _command(existing_connection_id="connection-existing", connection_credential_revision=5),
    )

    assert result.outcome == "connected"
    assert result.connection_id == "connection-existing"
    assert repository.created == []
    update = repository.updated[0]
    assert update["expected_credential_revision"] == 5
    assert update["connection_config"] == {
        "account_ref": "acct-1",
        "region": "south",
        "oauth": {"lifecycle_status": "connected", "token_type": "Bearer"},
    }
    # The submitted token replaces the stored one; the old one is not read.
    assert "resolve_refresh_token" not in repository.events
    assert token_store.stored_refresh_token == PASTED


def test_replacement_with_a_stale_revision_is_refused_before_the_exchange() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, token_store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceConflictError) as raised:
        service.import_refresh_token(
            _scope(),
            "install-a",
            "console",
            _command(
                existing_connection_id="connection-existing", connection_credential_revision=4
            ),
        )

    assert raised.value.payload == {"error": "oauth_connection_revision_conflict"}
    assert calls == []
    assert token_store.writes == []
    assert repository.updated == []


def test_replacement_racing_a_newer_revision_is_refused_by_the_commit() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)

    def refresh_then_race(**_values: Any) -> dict[str, Any]:
        repository.connections["connection-existing"]["credential_revision"] = 6
        return {"access_token": "access-token-canary", "token_type": "bearer"}

    service, _store, _calls = _service(repository, descriptor, refresh=refresh_then_race)

    with pytest.raises(ServiceConflictError) as raised:
        service.import_refresh_token(
            _scope(),
            "install-a",
            "console",
            _command(
                existing_connection_id="connection-existing", connection_credential_revision=5
            ),
        )

    assert raised.value.payload == {"error": "oauth_import_conflict"}
    assert repository.updated == []


def test_replacement_requires_its_revision() -> None:
    with pytest.raises(ValidationError):
        _command(existing_connection_id="connection-existing")
    with pytest.raises(ValidationError):
        _command(connection_credential_revision=5)


def test_validation_error_never_carries_the_token() -> None:
    with pytest.raises(ValidationError) as raised:
        OAuthRefreshTokenImport.model_validate(
            {
                "configuration_id": CONFIGURATION_ID,
                "connection_type": "console_account",
                "display_name": "Primary",
                "refresh_token": PASTED,
                "surprise": True,
            }
        )
    assert PASTED not in str(raised.value)
    assert PASTED not in repr(_command())


def test_authorization_code_provider_cannot_be_imported() -> None:
    descriptor = _manifest_fixture()
    repository = _Repository(_descriptor())
    service, _store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceValidationError) as raised:
        service.import_refresh_token(
            _scope(), "install-a", "example", _command(connection_type="example_mail")
        )

    assert raised.value.payload == {"error": "unsupported_oauth_flow"}
    assert calls == []


def test_declared_required_scope_must_be_granted() -> None:
    descriptor = _descriptor(required_scopes=["read"])
    repository = _Repository(descriptor)
    service, token_store, _calls = _service(
        repository,
        descriptor,
        refresh=lambda **_values: {
            "access_token": "access-token-canary",
            "token_type": "bearer",
            "scope": "other",
        },
    )

    with pytest.raises(ServicePreconditionError) as raised:
        service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert raised.value.payload["error"] == "oauth_scope_required"
    assert token_store.writes == []


def test_runtime_refresh_works_unchanged_on_an_imported_connection() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    service, token_store, _calls = _service(
        repository,
        descriptor,
        refresh=lambda **_values: {
            "access_token": "first-access",
            "token_type": "bearer",
            "expires_in": 10,
        },
    )
    imported = service.import_refresh_token(_scope(), "install-a", "console", _command())
    connection = repository.connections[imported.connection_id]
    refresh_calls: list[dict[str, Any]] = []

    class _RuntimeRepository:
        def lock_oauth_install_for_update(self, **scope: Any) -> dict[str, Any]:
            return repository.lock_oauth_install_for_update(**scope)

        def get_configuration_for_scope_for_update(self, **scope: Any) -> dict[str, Any]:
            return repository.get_configuration_for_scope_for_update(**scope)

        def lock_connection(self, **scope: Any) -> dict[str, Any]:
            return repository.lock_connection(**scope)

        @contextmanager
        def oauth_refresh_claim(self, **_scope: Any):
            yield

        def update_oauth_refresh(self, **values: Any) -> dict[str, Any]:
            connection["credential_revision"] += 1
            return {"connection_id": values.get("connection_id")}

    class _RuntimeTokens:
        def resolve_access_token(self, **_values: Any) -> str:
            return "first-access"

        def resolve_token_snapshot(self, **_values: Any) -> dict[str, Any]:
            return {
                "access_token": "first-access",
                "refresh_token": token_store.stored_refresh_token,
                "expires_at": 1005.0,
            }

        def write_tokens(self, **values: Any) -> dict[str, Any]:
            return token_store.write_tokens(**values)

    def runtime_refresh(**values: Any) -> dict[str, Any]:
        refresh_calls.append(copy.deepcopy(values))
        return {"access_token": "second-access", "token_type": "bearer", "expires_in": 3600}

    runtime = OAuthRuntimeTokenService(
        repository=_RuntimeRepository(),  # type: ignore[arg-type]
        token_store=_RuntimeTokens(),  # type: ignore[arg-type]
        install_loader=lambda _install, _account: {
            "extension_id": "fixture.oauth-import",
            "release_id": "release-a",
            "enabled": True,
            "compiled_manifest": copy.deepcopy(descriptor),
        },
        transaction_factory=contextmanager(lambda: (yield)),
        configuration_secret_value=lambda **_values: CLIENT_SECRET,
        refresh_access_token=runtime_refresh,
        now=lambda: 1000.0,
    )

    resolved = runtime.resolve_runtime_access_token(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.oauth-import",
        provider_id="console",
        configuration_id=CONFIGURATION_ID,
        connection_id=imported.connection_id,
        connection_type="console_account",
    )

    assert resolved == {"access_token": "second-access"}
    assert refresh_calls[0]["refresh_token"] == PASTED
    assert refresh_calls[0]["endpoint"].url == "https://identity.example.test/token"


# --- review fixes -----------------------------------------------------------


def _replace_command(**overrides: Any) -> OAuthRefreshTokenImport:
    return _command(
        existing_connection_id="connection-existing", connection_credential_revision=5, **overrides
    )


def _bound_repository(descriptor: dict[str, Any]) -> _Repository:
    repository = _Repository(descriptor)
    repository.connections["connection-existing"]["connection_config"]["oauth"] = {
        "lifecycle_status": "connected",
        "subject": "account-old",
        "verified_display_name": "Old account",
    }
    return repository


def _post_connect_returning(subject: str, *, health: str = "ok", error_code: str | None = None):
    def post_connect(**_values: Any) -> dict[str, Any]:
        if health != "ok":
            return {"status": "failed", "response": {"ok": False, "error_code": error_code}}
        return {
            "status": "succeeded",
            "response": {"result": {"subject": subject, "safe_metadata": {}, "health": "ok"}},
        }

    return post_connect


def test_replacement_verified_as_another_account_keeps_the_old_subject() -> None:
    descriptor = _descriptor()
    repository = _bound_repository(descriptor)
    service, _store, _calls = _service(
        repository, descriptor, post_connect=_post_connect_returning("account-other")
    )

    result = service.import_refresh_token(_scope(), "install-a", "console", _replace_command())

    assert result.outcome == "needs_attention"
    assert result.connection_id == "connection-existing"
    # While post-connect runs the connection still names its account, and Core
    # remembers which one it must stay bound to.
    pending = repository.updated[0]["connection_config"]["oauth"]
    assert pending["subject"] == "account-old"
    assert pending["replaced_subject"] == "account-old"
    recorded = repository.post_connect_updates[0]
    assert recorded["connection_config"]["oauth"]["subject"] == "account-old"
    assert recorded["connection_config"]["oauth"]["lifecycle_status"] == "needs_attention"
    assert recorded["connection_config"]["oauth"]["last_error_code"] == "subject_changed"
    assert "replaced_subject" not in recorded["connection_config"]["oauth"]
    assert recorded["last_error"] == "subject_changed"
    assert recorded["runtime_status"] == "unavailable"
    assert recorded["display_name"] == "Primary account"


def test_replacement_verified_as_the_same_account_connects() -> None:
    descriptor = _descriptor()
    repository = _bound_repository(descriptor)
    service, _store, _calls = _service(
        repository, descriptor, post_connect=_post_connect_returning("account-old")
    )

    result = service.import_refresh_token(_scope(), "install-a", "console", _replace_command())

    assert result.outcome == "connected"
    oauth = repository.post_connect_updates[0]["connection_config"]["oauth"]
    assert oauth["subject"] == "account-old"
    assert oauth["lifecycle_status"] == "connected"
    assert "replaced_subject" not in oauth


def test_failed_verification_of_an_imported_replacement_keeps_the_old_subject() -> None:
    descriptor = _descriptor()
    repository = _bound_repository(descriptor)
    service, _store, _calls = _service(
        repository,
        descriptor,
        post_connect=_post_connect_returning(
            "", health="failed", error_code="provider_unavailable"
        ),
    )

    result = service.import_refresh_token(_scope(), "install-a", "console", _replace_command())

    assert result.outcome == "needs_attention"
    oauth = repository.post_connect_updates[0]["connection_config"]["oauth"]
    assert oauth["subject"] == "account-old"
    assert oauth["last_error_code"] == "provider_unavailable"


def test_unknown_settings_keys_are_refused_even_when_the_schema_allows_extras() -> None:
    descriptor = _descriptor()
    schema = descriptor["runtime"]["extension_contract_v2"]["connection_types"][0]["config_schema"]
    schema["additionalProperties"] = True
    repository = _Repository(descriptor)
    service, _store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceValidationError) as raised:
        service.import_refresh_token(
            _scope(),
            "install-a",
            "console",
            _command(connection_config={"account_ref": "a", "region": "north", "extra": "x"}),
        )

    assert raised.value.payload == {
        "error": "invalid_connection_config",
        "field": "connection_config.extra",
    }
    assert calls == []


def test_a_type_without_config_schema_accepts_no_settings() -> None:
    descriptor = _descriptor()
    descriptor["runtime"]["extension_contract_v2"]["connection_types"][0].pop("config_schema")
    repository = _Repository(descriptor)
    service, _store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceValidationError) as raised:
        service.import_refresh_token(
            _scope(), "install-a", "console", _command(connection_config={"account_ref": "a"})
        )

    assert raised.value.payload == {
        "error": "invalid_connection_config",
        "field": "connection_config",
    }
    assert calls == []
    result = service.import_refresh_token(
        _scope(), "install-a", "console", _command(connection_config={})
    )
    assert result.outcome == "connected"
    assert set(repository.created[0]["connection_config"]) == {"oauth"}


def test_replacement_replaces_declared_settings_and_keeps_undeclared_ones() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    repository.connections["connection-existing"]["connection_config"]["legacy_key"] = "kept"
    service, _store, _calls = _service(repository, descriptor)

    service.import_refresh_token(
        _scope(),
        "install-a",
        "console",
        _replace_command(connection_config={"account_ref": "acct-2", "region": "north"}),
    )

    config = repository.updated[0]["connection_config"]
    assert config["legacy_key"] == "kept"
    assert config["account_ref"] == "acct-2"
    assert config["region"] == "north"


def test_stale_lineage_before_the_exchange_spends_no_token() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)
    # The first read builds the state; the configuration changes before the
    # pre-exchange check reads it again.
    repository.bump_configuration_after_reads = 1
    service, token_store, calls = _service(repository, descriptor)

    with pytest.raises(ServiceConflictError) as raised:
        service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert raised.value.payload == {"error": "oauth_import_conflict"}
    assert calls == []
    assert token_store.writes == []


def test_failed_commit_after_a_rotating_exchange_says_the_token_may_be_gone() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)

    def rotate_then_race(**_values: Any) -> dict[str, Any]:
        repository.connections["connection-existing"]["credential_revision"] = 6
        return {
            "access_token": "access-token-canary",
            "refresh_token": "rotated-refresh-token",
            "token_type": "bearer",
        }

    service, _store, _calls = _service(repository, descriptor, refresh=rotate_then_race)

    with pytest.raises(ServiceConflictError) as raised:
        service.import_refresh_token(_scope(), "install-a", "console", _replace_command())

    assert raised.value.payload == {"error": "oauth_import_conflict_token_rotated"}
    assert "rotated-refresh-token" not in str(raised.value)


def test_omitted_scope_records_only_the_required_scopes() -> None:
    descriptor = _descriptor(required_scopes=["read"])
    descriptor["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"]["scopes"][
        "optional"
    ] = ["write"]
    repository = _Repository(descriptor)
    service, _store, _calls = _service(repository, descriptor)

    service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert repository.created[0]["oauth_granted_scopes"] == ["read"]
    assert repository.created[0]["oauth_requested_scopes"] == ["read", "write"]


def test_invalid_client_marks_the_configuration_and_keeps_412() -> None:
    descriptor = _descriptor()
    repository = _Repository(descriptor)

    def refuse(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(OAuthErrorCode.INVALID_CLIENT, OAuthProviderOperation.REFRESH)

    service, token_store, _calls = _service(repository, descriptor, refresh=refuse)

    with pytest.raises(ServicePreconditionError) as raised:
        service.import_refresh_token(_scope(), "install-a", "console", _command())

    assert raised.value.status_code == 412
    assert raised.value.payload["error"] == "oauth_invalid_client"
    assert repository.invalid_client_marks[0]["expected_configuration_revision"] == 4
    assert token_store.writes == []


def test_grant_repr_and_parse_errors_never_carry_the_token() -> None:
    grant = OAuthGrant(
        token_response={"access_token": "access-canary", "refresh_token": PASTED},
        requested_scopes=[],
        granted_scopes=[],
        connection_id="connection-a",
        fallback_refresh_token=PASTED,
    )
    assert PASTED not in repr(grant)
    assert "access-canary" not in str(grant)

    with pytest.raises(ServiceValidationError) as raised:
        parse_refresh_token_import(
            {
                "configuration_id": CONFIGURATION_ID,
                "connection_type": "console_account",
                "display_name": "Primary",
                "refresh_token": PASTED,
                "existing_connection_id": "connection-a",
            }
        )
    error = raised.value
    assert error.payload == {"error": "invalid_request"}
    assert error.__cause__ is None
    assert error.__context__ is None
    assert PASTED not in str(error)


@pytest.mark.parametrize("minimum", [None, "1.0.0", "1.5.0"])
def test_import_provider_requires_its_host_contract_minimum(minimum) -> None:
    descriptor = _descriptor()
    if minimum is None:
        descriptor["runtime"].pop("compatibility")
    else:
        descriptor["runtime"]["compatibility"]["platform_min"] = minimum

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "oauth_import_host_minimum" in _error_codes(result)


def test_import_bundle_is_accepted_by_the_shipping_host(monkeypatch) -> None:
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )

    monkeypatch.delenv("FS_VERSION", raising=False)
    descriptor = _descriptor()
    assert validate_extension_contract_v2(descriptor)["valid"] is True
    assert not _platform_compatibility_error(
        descriptor, platform_version=_runtime_platform_version()
    )


def test_import_bundle_is_refused_by_an_explicit_older_host(monkeypatch) -> None:
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )

    monkeypatch.setenv("FS_VERSION", "1.5.0")
    error = _platform_compatibility_error(
        _descriptor(), platform_version=_runtime_platform_version()
    )
    assert "below required minimum" in error
    assert "1.6.0" in error


def test_shipping_host_keeps_older_bundles_without_a_maximum_compatible(monkeypatch) -> None:
    from core.application.extensions.service_support import (
        _platform_compatibility_error,
        _runtime_platform_version,
    )

    monkeypatch.delenv("FS_VERSION", raising=False)
    descriptor = {"runtime": {"compatibility": {"platform_min": "1.5.0"}}}
    assert not _platform_compatibility_error(
        descriptor, platform_version=_runtime_platform_version()
    )
