"""OAuth provider declarations, credentials and pinned endpoint exchanges."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request

from core.application.auth.oauth_client_values import (
    OAUTH_ACCESS_TOKEN_SECRET_DEFAULT,
    OAuthConnectionError,
    OAuthTokenEndpointError,
    _as_dict,
    _as_string_list,
    _as_text,
)
from core.application.extensions import url_safety
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _assert_https_unless_dev(url: str, *, purpose: str) -> None:
    scheme = str(urlparse(url).scheme or "").lower()
    if scheme != "https" and not url_safety._allow_private_remote_urls():
        raise OAuthConnectionError(f"{purpose} must use https")


def _validated_endpoint(auth_config: dict[str, Any], key: str, *, required: bool) -> str:
    value = _as_text(auth_config.get(key))
    if not value:
        if required:
            raise OAuthConnectionError(f"OAuth auth.{key} is required")
        return ""
    if not _is_http_url(value):
        raise OAuthConnectionError(f"OAuth auth.{key} must be an http(s) URL")
    try:
        _assert_https_unless_dev(value, purpose=f"OAuth auth.{key}")
        parsed = urlparse(value)
        if parsed.username or parsed.password:
            raise OAuthConnectionError(f"OAuth auth.{key} must not include URL credentials")
    except OAuthConnectionError:
        raise
    except Exception as exc:
        raise OAuthConnectionError(str(exc)) from exc
    return value


def normalize_oauth_auth_config(auth_config: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(auth_config)
    if _as_text(payload.get("type")).lower() != "oauth2":
        raise OAuthConnectionError("Connection type auth.type must be oauth2")
    if _as_text(payload.get("grant")).lower() != "authorization_code":
        raise OAuthConnectionError("OAuth grant must be authorization_code")
    if _as_text(payload.get("pkce")).lower() != "required":
        raise OAuthConnectionError("OAuth PKCE must be required")
    token_placement = _as_text(payload.get("token_placement")).lower() or "secrets"
    if token_placement != "secrets":
        raise OAuthConnectionError("OAuth token_placement must be secrets")

    scopes = _as_dict(payload.get("scopes"))
    secret_names = _as_dict(payload.get("secret_names"))
    client_credentials = _as_dict(payload.get("client_credentials"))
    normalized = dict(payload)
    normalized["authorization_endpoint"] = _validated_endpoint(
        payload, "authorization_endpoint", required=True
    )
    normalized["token_endpoint"] = _validated_endpoint(payload, "token_endpoint", required=True)
    normalized["revocation_endpoint"] = _validated_endpoint(
        payload, "revocation_endpoint", required=False
    )
    normalized["scopes"] = {
        "required": _as_string_list(scopes.get("required")),
        "optional": _as_string_list(scopes.get("optional")),
    }
    normalized["client_credentials"] = {
        "client_id": _as_dict(client_credentials.get("client_id")),
        "client_secret": _as_dict(client_credentials.get("client_secret")),
    }
    normalized["token_placement"] = "secrets"
    normalized["secret_names"] = {
        "access_token": _as_text(secret_names.get("access_token"))
        or OAUTH_ACCESS_TOKEN_SECRET_DEFAULT
    }
    refresh = _as_dict(payload.get("refresh"))
    normalized["refresh"] = {
        "rotates_refresh_token": bool(refresh.get("rotates_refresh_token", False))
    }
    return normalized


def _selected_scopes(auth_config: dict[str, Any], optional_scopes: list[str] | None) -> list[str]:
    scopes = _as_dict(auth_config.get("scopes"))
    required = _as_string_list(scopes.get("required"))
    optional_allowed = set(_as_string_list(scopes.get("optional")))
    selected = list(required)
    for scope in _as_string_list(optional_scopes or []):
        if scope in optional_allowed and scope not in selected:
            selected.append(scope)
    return selected


def _credential_env_name(credential: dict[str, Any]) -> str:
    return _as_text(
        credential.get("env")
        or credential.get("env_name")
        or credential.get("name")
        or credential.get("secret_name")
    )


def _connection_oauth_config(connection: dict[str, Any]) -> dict[str, Any]:
    config = _as_dict(connection.get("connection_config"))
    oauth_config = _as_dict(config.get("oauth"))
    return {**config, **oauth_config}


def _client_credential_value(
    *,
    credential_store: Any,
    auth_config: dict[str, Any],
    connection: dict[str, Any],
    credential_name: str,
    required: bool,
) -> str:
    del credential_store
    client_credentials = _as_dict(auth_config.get("client_credentials"))
    credential = _as_dict(client_credentials.get(credential_name))
    provider_key = _as_text(credential.get("provider_key")).lower()
    if provider_key:
        raise OAuthConnectionError(f"OAuth {credential_name} uses an unsupported provider_key")
    source = _as_text(credential.get("source")).lower()
    if source and source != "platform_secret":
        raise OAuthConnectionError(f"OAuth {credential_name} must use source=platform_secret")

    env_name = _credential_env_name(credential)
    if env_name:
        value = _as_text(_runtime_env.get(env_name))
        if value:
            return value
        if required:
            raise OAuthConnectionError(f"OAuth platform secret env '{env_name}' is not set")
        return ""

    connection_config = _connection_oauth_config(connection)
    if credential_name == "client_id":
        value = _as_text(
            connection_config.get("oauth_client_id") or connection_config.get("client_id")
        )
        if value:
            return value

    if required:
        raise OAuthConnectionError(f"OAuth {credential_name} platform secret is not configured")
    return ""


def _provider_credential_generation(
    credential_store: Any,
    auth_config: dict[str, Any],
    *,
    connection: dict[str, Any],
) -> int:
    specs = _as_dict(auth_config.get("client_credentials"))
    provider_keys = {
        _as_text(_as_dict(specs.get(name)).get("provider_key")).lower()
        for name in ("client_id", "client_secret")
        if _as_text(_as_dict(specs.get(name)).get("provider_key"))
    }
    if provider_keys:
        raise OAuthConnectionError("OAuth client credentials must use platform_secret")
    # Extension-owned credentials are resolved from the validated connection
    # declaration and never from a host-wide provider record.
    _ = credential_store, connection
    return 0


def _access_token_secret_name(auth_config: dict[str, Any]) -> str:
    secret_names = _as_dict(auth_config.get("secret_names"))
    return _as_text(secret_names.get("access_token")) or OAUTH_ACCESS_TOKEN_SECRET_DEFAULT


def oauth_auth_config_for_connection(bl: Any, connection: dict[str, Any]) -> dict[str, Any]:
    extension_id = _as_text(connection.get("extension_id"))
    connection_type = _as_text(connection.get("connection_type"))
    if not extension_id or not connection_type:
        return {}

    try:
        from core.application.extensions.extension_contract_v2 import (
            validate_extension_contract_v2,
        )
        from core.infrastructure.extension_runtime.extension_filesystem_loader import (
            load_extension_from_filesystem,
        )
    except Exception:
        return {}

    extension_row: dict[str, Any] = {}
    getter = getattr(bl, "get_extension", None)
    if callable(getter):
        try:
            candidate = getter(
                extension_id, account_id=_as_text(connection.get("account_id")) or None
            )
        except TypeError:
            candidate = getter(extension_id)
        except Exception:
            candidate = None
        if isinstance(candidate, dict):
            extension_row = dict(candidate)

    if not extension_row:
        # Legacy persisted connection rows can predate the canonical package-ID
        # contract. They are not OAuth-capable, and must not be passed into the
        # filesystem loader whose direct-path mapping intentionally validates
        # new extension identifiers strictly.
        try:
            from core.application.extensions.identity import (
                extension_folder_name,
                normalize_extension_id,
            )

            normalize_extension_id(extension_id)
        except ValueError:
            return {}
        candidate = load_extension_from_filesystem(
            extension_id,
            canonical_folder_name=extension_folder_name(extension_id),
        )
        if isinstance(candidate, dict):
            extension_row = dict(candidate)
    if not extension_row:
        return {}

    manifest = _as_dict(extension_row.get("manifest"))
    if not manifest:
        manifest = _as_dict(extension_row.get("compiled_manifest"))
    bundle_root = _as_text(extension_row.get("bundle_root"))
    validation = validate_extension_contract_v2(
        manifest,
        bundle_root=bundle_root or None,
    )
    if validation.get("valid") is not True:
        return {}
    contract = _as_dict(validation.get("normalized_contract"))
    for row in contract.get("connection_types") or []:
        payload = _as_dict(row)
        row_type = _as_text(payload.get("connection_type_id"))
        if row_type != connection_type:
            continue
        auth = _as_dict(payload.get("auth"))
        if _as_text(auth.get("type")).lower() == "oauth2":
            return normalize_oauth_auth_config(auth)
    return {}


def _http_post_form(
    url: str,
    data: dict[str, str],
    *,
    timeout_seconds: float = 15.0,
    purpose: str = "OAuth token endpoint",
) -> dict[str, Any]:
    _assert_https_unless_dev(url, purpose=purpose)
    encoded = urlencode(data).encode("utf-8")
    # The request is opened through url_safety.open_pinned_url below.
    request = Request(  # nosec B310
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with url_safety.open_pinned_url(
            request,
            timeout_seconds=max(1.0, float(timeout_seconds)),
            purpose=purpose,
        ) as response:  # nosec B310
            try:
                raw_bytes = response.read(65_537)
            except TypeError:
                # Minimal test doubles may expose read() without the stdlib size argument.
                raw_bytes = response.read()
            if len(raw_bytes) > 65_536:
                raise OAuthConnectionError("OAuth token endpoint response is too large")
            raw = raw_bytes.decode("utf-8")
    except HTTPError as exc:
        raw_body = b""
        try:
            raw_body = exc.read(16_385)
        except Exception:
            raw_body = b""
        error_code = "token_endpoint_error"
        if len(raw_body) <= 16_384:
            with contextlib.suppress(Exception):
                error_payload = json.loads(raw_body.decode("utf-8") or "{}")
                if isinstance(error_payload, dict):
                    error_code = _as_text(error_payload.get("error")) or error_code
        raise OAuthTokenEndpointError(
            error_code=error_code,
            status_code=int(exc.code),
        ) from exc
    except ValueError as exc:
        raise OAuthConnectionError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, OAuthConnectionError):
            raise
        raise OAuthConnectionError(f"{purpose} request failed") from exc
    try:
        parsed = json.loads(raw or "{}")
    except Exception as exc:
        raise OAuthConnectionError("OAuth token endpoint did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise OAuthConnectionError("OAuth token endpoint returned invalid JSON")
    return parsed


def exchange_authorization_code(
    *,
    auth_config: dict[str, Any],
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
    client_secret: str = "",
    http_post_form: Callable[..., dict[str, Any]] = _http_post_form,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    token_params = _as_dict(auth_config.get("token_params"))
    for key, value in token_params.items():
        if key not in data and value is not None:
            data[str(key)] = str(value)
    return http_post_form(auth_config["token_endpoint"], data)


def exchange_refresh_token(
    *,
    auth_config: dict[str, Any],
    refresh_token: str,
    client_id: str,
    client_secret: str = "",
    http_post_form: Callable[..., dict[str, Any]] = _http_post_form,
) -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    token_params = _as_dict(auth_config.get("token_params"))
    for key, value in token_params.items():
        if key not in data and value is not None:
            data[str(key)] = str(value)
    return http_post_form(auth_config["token_endpoint"], data)
