"""Validation and discovery policy for MCP OAuth configuration."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.application.management.mcp_connector_common import (
    _normalize_auth_mode,
    _normalize_string_list,
)

from .errors import (
    ServiceValidationError,
)

_MCP_OAUTH_KEY_MAX_LENGTH = 128

_MCP_OAUTH_VALUE_MAX_LENGTH = 512

_MCP_OAUTH_RESERVED_PARAMETER_IDENTITIES = frozenset(
    {
        "responsetype",
        "clientid",
        "redirecturi",
        "state",
        "codechallenge",
        "codechallengemethod",
        "scope",
        "resource",
        "clientsecret",
        "authorization",
        "apikey",
        "password",
        "secret",
        "token",
        "idtoken",
        "accesstoken",
        "refreshtoken",
        "code",
        "codeverifier",
        "clientassertion",
        "clientassertiontype",
    }
)

_MCP_OAUTH_CREDENTIAL_MARKERS = (
    "bearer",
    "credential",
    "secret",
    "password",
    "passwd",
    "token",
    "apikey",
    "privatekey",
)

_MCP_OAUTH_SAFE_PARAMETER_IDENTITIES = frozenset({"tokenaccesstype"})


def _annotate_oauth_scope_availability(
    connector_row: dict[str, Any],
    discovered_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fail closed when an existing OAuth grant trails connector requirements."""
    tools = [dict(tool) for tool in discovered_tools]
    if _normalize_auth_mode(connector_row.get("auth_mode")) == "oauth2":
        metadata = connector_row.get("oauth_metadata_jsonb")
        metadata = metadata if isinstance(metadata, dict) else {}
        requested = set(_normalize_string_list(metadata.get("requested_scopes")))
        token = metadata.get("token")
        token = token if isinstance(token, dict) else {}
        granted = set(_normalize_string_list(token.get("granted_scopes")))
        missing = sorted(requested - granted)
        if missing:
            reason = (
                "Reconnect this OAuth connector to grant its current required scopes: "
                + ", ".join(missing)
            )
            for tool in tools:
                tool.update(
                    {
                        "can_publish": False,
                        "lifecycle_state": "unavailable",
                        "availability_reason_code": "oauth_reauthorization_required",
                        "availability_reason_text": reason,
                        "missing_oauth_scopes": missing,
                        "recovery_action": "reconnect_oauth",
                    }
                )
    return tools


def _oauth_parameter_identity(value: Any) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _oauth_parameter_is_reserved(value: Any) -> bool:
    identity = _oauth_parameter_identity(value)
    if identity in _MCP_OAUTH_SAFE_PARAMETER_IDENTITIES:
        return False
    return identity in _MCP_OAUTH_RESERVED_PARAMETER_IDENTITIES or any(
        marker in identity for marker in _MCP_OAUTH_CREDENTIAL_MARKERS
    )


def _oauth_metadata_from_config(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    forbidden_manual_fields = {
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "revocation_endpoint",
        "registration_endpoint",
        "scopes_supported",
    }
    if any(str(key or "").strip().lower() in forbidden_manual_fields for key in payload):
        raise ServiceValidationError(
            "OAuth endpoints and issuer are discovered automatically and cannot be overridden",
            payload={
                "error": "oauth_config_override_forbidden",
                "field": "authorization_parameters",
                "message": "OAuth endpoints and issuer are discovered automatically and cannot be overridden",
            },
        )
    metadata: dict[str, Any] = {}
    if "requested_scopes" in payload:
        raw_scopes = payload["requested_scopes"]
        if not isinstance(raw_scopes, list):
            raise ServiceValidationError(
                "oauth_config.requested_scopes must be an array",
                payload={
                    "error": "invalid_oauth_scopes",
                    "field": "requested_scopes",
                    "message": "oauth_config.requested_scopes must be an array",
                },
            )
        scopes: list[str] = []
        for item in raw_scopes:
            if not isinstance(item, str) or not item.strip():
                raise ServiceValidationError(
                    "oauth_config.requested_scopes must contain strings",
                    payload={
                        "error": "invalid_oauth_scopes",
                        "field": "requested_scopes",
                        "message": "oauth_config.requested_scopes must contain strings",
                    },
                )
            scope = item.strip()
            if len(scope) > _MCP_OAUTH_VALUE_MAX_LENGTH or scope in scopes:
                raise ServiceValidationError(
                    "oauth_config.requested_scopes contains invalid values",
                    payload={
                        "error": "invalid_oauth_scopes",
                        "field": "requested_scopes",
                        "message": "oauth_config.requested_scopes contains invalid values",
                    },
                )
            scopes.append(scope)
        metadata["requested_scopes"] = scopes
    if "authorization_parameters" in payload:
        raw_parameters = payload["authorization_parameters"]
        if not isinstance(raw_parameters, dict):
            raise ServiceValidationError(
                "oauth_config.authorization_parameters must be an object",
                payload={
                    "error": "invalid_oauth_parameters",
                    "field": "authorization_parameters",
                    "message": "oauth_config.authorization_parameters must be an object",
                },
            )
        parameters: dict[str, str] = {}
        seen_keys: set[str] = set()
        for raw_key, raw_value in raw_parameters.items():
            key = str(raw_key or "").strip()
            key_identity = _oauth_parameter_identity(key)
            if (
                not key
                or len(key) > _MCP_OAUTH_KEY_MAX_LENGTH
                or key_identity in seen_keys
                or _oauth_parameter_is_reserved(key)
                or not isinstance(raw_value, (str, int, float, bool))
            ):
                raise ServiceValidationError(
                    "oauth_config.authorization_parameters contains an invalid or reserved key",
                    payload={
                        "error": "invalid_oauth_parameters",
                        "field": "authorization_parameters",
                        "message": "oauth_config.authorization_parameters contains an invalid or reserved key",
                    },
                )
            text_value = str(raw_value)
            if len(text_value) > _MCP_OAUTH_VALUE_MAX_LENGTH:
                raise ServiceValidationError(
                    "oauth_config.authorization_parameters contains an oversized value",
                    payload={
                        "error": "invalid_oauth_parameters",
                        "field": "authorization_parameters",
                        "message": "oauth_config.authorization_parameters contains an oversized value",
                    },
                )
            seen_keys.add(key_identity)
            parameters[key] = text_value
        metadata["authorization_parameters"] = parameters
    if "refresh_token_required" in payload:
        if type(payload["refresh_token_required"]) is not bool:
            raise ServiceValidationError(
                "oauth_config.refresh_token_required must be boolean",
                payload={
                    "error": "invalid_refresh_token_requirement",
                    "field": "refresh_token_required",
                    "message": "oauth_config.refresh_token_required must be boolean",
                },
            )
        metadata["refresh_token_required"] = payload["refresh_token_required"]
    return metadata


def _validate_oauth_headers(auth_mode: str, headers: Any) -> None:
    if str(auth_mode or "").strip().lower() != "oauth2":
        return
    normalized_headers = headers if isinstance(headers, dict) else {}
    forbidden_names = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "apikey",
    }
    for key in normalized_headers:
        if str(key or "").strip().lower() in forbidden_names:
            raise ServiceValidationError(
                "OAuth2 MCP connectors must not store credential headers; use OAuth connect",
                payload={
                    "error": "oauth_header_collision",
                    "field": "headers",
                    "message": "OAuth2 MCP connectors must not store credential headers; use OAuth connect",
                },
            )


_MCP_SYNC_OAUTH_CONFIGURATION_FIELDS = (
    "requested_scopes",
    "authorization_parameters",
    "refresh_token_required",
)


_MCP_SYNC_OAUTH_APPLICATION_SECRET_NAMES = (
    "_oauth_client_id",
    "_oauth_client_secret",
    "_oauth_registration_access_token",
)


def _sync_configuration_fingerprint(row: dict[str, Any]) -> str:
    oauth_metadata = row.get("oauth_metadata_jsonb")
    oauth_metadata = oauth_metadata if isinstance(oauth_metadata, dict) else {}
    oauth_secret_refs = row.get("oauth_secret_refs_jsonb")
    oauth_secret_refs = oauth_secret_refs if isinstance(oauth_secret_refs, dict) else {}
    application_secret_refs = oauth_secret_refs.get("refs")
    application_secret_refs = (
        application_secret_refs if isinstance(application_secret_refs, dict) else {}
    )
    payload = {
        key: row.get(key)
        for key in (
            "account_id",
            "project_id",
            "tenant_id",
            "transport",
            "base_url",
            "auth_mode",
            "header_secret_refs_jsonb",
            "oauth_credential_revision",
            "oauth_connected_identity",
            "namespace_prefix",
            "allowlisted_tools",
            "enabled",
        )
    }
    payload["oauth_configuration"] = {
        key: oauth_metadata.get(key) for key in _MCP_SYNC_OAUTH_CONFIGURATION_FIELDS
    }
    payload["oauth_application_secret_refs"] = {
        key: application_secret_refs.get(key) for key in _MCP_SYNC_OAUTH_APPLICATION_SECRET_NAMES
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
