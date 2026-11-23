"""Connection credentials borrowed from an OAuth configuration (credentials_from)."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from core.application.extensions.extension_contract_references import (
    _validate_connection_type_credentials_from,
)
from core.application.extensions.oauth_contract import compile_oauth_providers

PROVIDER = {
    "provider_id": "example_oauth",
    "credential_fields": [
        {"field_id": "client_id", "secret": False},
        {"field_id": "client_secret", "secret": True},
        {"field_id": "tenant_id", "secret": False},
    ],
}


def _errors(connection_type: dict[str, Any]) -> list[str]:
    errors: list[dict[str, str]] = []
    _validate_connection_type_credentials_from(
        connection_type, providers=[PROVIDER], idx=0, errors=errors
    )
    return [error["code"] for error in errors]


VALID = {
    "connection_type_id": "example_app",
    "config_schema": {"type": "object", "properties": {"account_ref": {"type": "string"}}},
    "credentials_from": {
        "oauth_provider_id": "example_oauth",
        "fields": {
            "client_id": {"into": "config", "as": "client_id"},
            "client_secret": {"into": "secrets", "as": "client_secret"},
            "tenant_id": {"into": "config", "as": "tenant_id"},
        },
    },
}


def test_a_valid_credentials_from_compiles() -> None:
    assert _errors(VALID) == []


def _with(**changes: Any) -> dict[str, Any]:
    connection_type = copy.deepcopy(VALID)
    connection_type["credentials_from"].update(changes)
    return connection_type


@pytest.mark.parametrize(
    ("connection_type", "code"),
    [
        (_with(oauth_provider_id="other_oauth"), "unknown_credentials_from_provider"),
        (
            _with(fields={"region": {"into": "config", "as": "region"}}),
            "unknown_credentials_from_field",
        ),
        (
            _with(fields={"client_secret": {"into": "config", "as": "client_secret"}}),
            "credentials_from_secret_into_config",
        ),
        (
            _with(fields={"client_id": {"into": "config", "as": "account_ref"}}),
            "credentials_from_name_collision",
        ),
        (
            {**copy.deepcopy(VALID), "auth": {"type": "oauth2", "provider_id": "example_oauth"}},
            "invalid_credentials_from",
        ),
    ],
    ids=["unknown-provider", "unknown-field", "secret-into-config", "name-collision", "oauth-type"],
)
def test_an_invalid_credentials_from_is_rejected(connection_type, code) -> None:
    assert code in _errors(connection_type)


def _runtime_descriptor() -> dict[str, Any]:
    from tests.unit.test_extension_oauth_authorization import _descriptor

    descriptor = _descriptor()
    provider = descriptor["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    provider["credential_fields"].append(
        {
            "field_id": "tenant_id",
            "value_type": "text",
            "required": True,
            "secret": False,
            "change_policy": "runtime_revalidate",
            "usage": "runtime",
            "label": "Directory ID",
            "help_text": "Used by connections that sign in as the application.",
            "validation": {"min_length": 1, "max_length": 200},
        }
    )
    return descriptor


def test_a_runtime_field_compiles_and_is_published_with_its_usage() -> None:
    [provider] = compile_oauth_providers(_runtime_descriptor())
    tenant = next(field for field in provider.credential_fields if field.field_id == "tenant_id")
    assert tenant.runtime_only
    assert tenant.to_dict()["usage"] == "runtime"
    client_id = next(field for field in provider.credential_fields if field.field_id == "client_id")
    # Existing fields keep their published shape and fingerprint.
    assert "usage" not in client_id.to_dict()
    assert "usage" not in client_id.security_dict()


def test_client_roles_cannot_be_runtime_only() -> None:
    descriptor = _runtime_descriptor()
    provider = descriptor["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    provider["credential_fields"][0]["usage"] = "runtime"
    with pytest.raises(Exception, match=r"invalid_oauth_credential_field|runtime-only"):
        compile_oauth_providers(descriptor)


def test_a_runtime_field_is_never_sent_in_the_authorization_request() -> None:
    from tests.unit.test_extension_oauth_authorization import (
        _command,
        _Repository,
        _scope,
        _service,
    )

    repository = _Repository()
    repository.configuration["non_secret_config"] = {
        "client_id": "declared-client",
        "tenant_id": "runtime-tenant-7",
    }
    result = _service(repository, descriptor=_runtime_descriptor()).start_authorization(
        _scope(), "install-a", "example", _command()
    )

    query = parse_qs(urlsplit(result.authorization_url).query)
    assert "runtime-tenant-7" not in result.authorization_url
    assert "tenant_id" not in query
    assert query["client_id"] == ["declared-client"]


def test_the_authorization_flow_does_not_need_a_runtime_field() -> None:
    from tests.unit.test_extension_oauth_authorization import (
        _command,
        _Repository,
        _scope,
        _service,
    )

    repository = _Repository()  # no tenant_id stored
    result = _service(repository, descriptor=_runtime_descriptor()).start_authorization(
        _scope(), "install-a", "example", _command()
    )
    assert result.authorization_url
