"""Pure scope, credential and confirmation policies for OAuth configurations."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.oauth_configuration_models import (
    OAuthConfigurationSave,
    _PreparedChange,
    _ResolvedProvider,
)
from core.application.extensions.oauth_contract import (
    OAuthCredentialField,
    is_canonical_https_origin,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

_MESSAGE_CONFIGURATION_NOT_FOUND = "Configuration not found"


def _scope_values(scope: ScopeContext) -> dict[str, str]:
    tenant_id = str(scope.tenant_id or "").strip()
    account_id = str(scope.account_id or "").strip()
    project_id = str(scope.project_id or "").strip()
    if not tenant_id or not account_id or not project_id:
        raise ServiceNotFoundError(_MESSAGE_CONFIGURATION_NOT_FOUND)
    return {
        "tenant_id": tenant_id,
        "account_id": account_id,
        "project_id": project_id,
    }


def _require_role(scope: ScopeContext, role: str) -> None:
    if not principal_type_has_minimum_role(scope.principal_type, role):
        label = "Operator" if role == "operator" else "Viewer"
        raise ServiceForbiddenError(f"{label} role required")


def _raw_repository_scope(
    scope: ScopeContext,
    install_id: str,
    extension_id: str,
    provider_id: str,
) -> dict[str, str]:
    return {
        **_scope_values(scope),
        "extension_install_id": str(install_id or "").strip(),
        "extension_id": extension_id,
        "provider_id": str(provider_id or "").strip(),
    }


def _repository_scope(
    scope: ScopeContext,
    install_id: str,
    resolved: _ResolvedProvider,
) -> dict[str, str]:
    return {
        **_scope_values(scope),
        "extension_install_id": str(install_id or "").strip(),
        "extension_id": resolved.extension_id,
        "provider_id": resolved.provider.provider_id,
    }


def _refs(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    refs = value.get("refs")
    source: Mapping[Any, Any] = refs if isinstance(refs, Mapping) else value
    return {
        str(key): str(ref)
        for key, ref in source.items()
        if str(key or "").strip() and str(ref or "").strip()
    }


def _validate_value(field: OAuthCredentialField, value: str, *, present: bool) -> None:
    if field.required and not present:
        raise ServiceValidationError(
            f"{field.field_id} is required",
            payload={
                "error": "invalid_credential_field",
                "field_id": field.field_id,
                "reason": "required",
            },
        )
    if not present:
        return
    minimum = int(field.validation.get("min_length", 0))
    maximum = int(field.validation.get("max_length", 4096))
    if len(value) < minimum:
        raise ServiceValidationError(
            f"{field.field_id} is too short",
            payload={
                "error": "invalid_credential_field",
                "field_id": field.field_id,
                "reason": "too_short",
            },
        )
    if len(value) > maximum:
        raise ServiceValidationError(
            f"{field.field_id} is too long",
            payload={
                "error": "invalid_credential_field",
                "field_id": field.field_id,
                "reason": "too_long",
            },
        )
    if field.validation.get("type") == "https_origin" and not is_canonical_https_origin(value):
        raise ServiceValidationError(
            f"{field.field_id} has an invalid format",
            payload={
                "error": "invalid_credential_field",
                "field_id": field.field_id,
                "reason": "invalid_format",
            },
        )


def _require_current_contract(existing: Mapping[str, Any], resolved: _ResolvedProvider) -> None:
    current = (
        str(existing.get("extension_release_id") or "") == resolved.release_id
        and str(existing.get("provider_security_fingerprint") or "")
        == provider_security_fingerprint(resolved.provider)
        and str(existing.get("provider_revocation_fingerprint") or "")
        == provider_revocation_fingerprint(resolved.provider)
    )
    if not current:
        raise ServicePreconditionError(
            "OAuth release transition approval required",
            payload={"error": "oauth_release_transition_required"},
        )


def _confirmation_token(
    confirmation_key: bytes,
    scope: ScopeContext,
    install_id: str,
    provider_id: str,
    command: OAuthConfigurationSave,
    expected_revision: int | None,
    prepared: _PreparedChange,
) -> str:
    candidate_digest = hmac.new(
        confirmation_key,
        json.dumps(
            {
                "display_name": command.display_name,
                "values": prepared.non_secret_config,
                "secret_replacements": prepared.secret_replacements,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    payload = json.dumps(
        {
            **_scope_values(scope),
            "install_id": install_id,
            "extension_id": prepared.resolved.extension_id,
            "provider_id": provider_id,
            "configuration_id": prepared.configuration_id,
            "expected_revision": expected_revision,
            "candidate_digest": candidate_digest,
            "connection_ids": sorted(
                str(row.get("connection_id") or "") for row in prepared.connections
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(confirmation_key, payload, hashlib.sha256).hexdigest()


def _delete_confirmation_token(
    confirmation_key: bytes,
    scope: ScopeContext,
    install_id: str,
    extension_id: str,
    provider_id: str,
    configuration_id: str,
    expected_revision: int,
    connections: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "action": "delete",
            **_scope_values(scope),
            "install_id": install_id,
            "extension_id": extension_id,
            "provider_id": provider_id,
            "configuration_id": configuration_id,
            "expected_revision": expected_revision,
            "connection_ids": sorted(str(row.get("connection_id") or "") for row in connections),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(confirmation_key, payload, hashlib.sha256).hexdigest()


def _reject_disconnecting_connections(connections: list[dict[str, Any]]) -> None:
    def lifecycle_status(connection: Mapping[str, Any]) -> str:
        config = connection.get("connection_config")
        oauth = config.get("oauth") if isinstance(config, Mapping) else None
        return str(oauth.get("lifecycle_status") or "") if isinstance(oauth, Mapping) else ""

    connection_ids = sorted(
        str(connection.get("connection_id") or "")
        for connection in connections
        if lifecycle_status(connection) == "disconnecting"
    )
    connection_ids = [connection_id for connection_id in connection_ids if connection_id]
    if connection_ids:
        raise ServicePreconditionError(
            "OAuth configuration cannot change while disconnects are in progress",
            payload={
                "error": "oauth_configuration_disconnect_in_progress",
                "connection_ids": connection_ids,
            },
        )
