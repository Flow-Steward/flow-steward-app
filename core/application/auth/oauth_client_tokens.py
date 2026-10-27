"""Persist OAuth tokens and refresh them under the connection claim."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.application.auth.oauth_client_provider import (
    _access_token_secret_name,
    _client_credential_value,
    _http_post_form,
    _provider_credential_generation,
    exchange_refresh_token,
    normalize_oauth_auth_config,
    oauth_auth_config_for_connection,
)
from core.application.auth.oauth_client_values import (
    OAUTH_EXPIRES_AT_SECRET,
    OAUTH_GRANTED_SCOPES_SECRET,
    OAUTH_REFRESH_SKEW_SECONDS,
    OAUTH_REFRESH_TOKEN_SECRET,
    OAUTH_TOKEN_TYPE_SECRET,
    OAuthConnectionError,
    OAuthTokenEndpointError,
    _as_dict,
    _as_string_list,
    _as_text,
    _now,
    _OAuthRefreshStaleError,
)


def _token_expires_at(token_response: dict[str, Any], *, now: float | None = None) -> float:
    try:
        expires_in = int(token_response.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return float(now if now is not None else _now()) + max(1, expires_in)


def _granted_scopes(token_response: dict[str, Any], fallback_scopes: list[str]) -> list[str]:
    raw_scope = token_response.get("scope")
    if isinstance(raw_scope, str):
        parsed = [item.strip() for item in raw_scope.replace(",", " ").split() if item.strip()]
        return list(dict.fromkeys(parsed))
    if isinstance(raw_scope, list):
        return _as_string_list(raw_scope)
    return list(fallback_scopes)


def store_oauth_token_response(
    conn: Any,
    *,
    connection: dict[str, Any],
    auth_config: dict[str, Any],
    token_response: dict[str, Any],
    granted_scope_fallback: list[str],
    existing_refresh_token: str = "",
    oauth_metadata: dict[str, Any] | None = None,
    bump_credential_revision: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    from core.application.management import project_connection_secrets
    from core.application.management.project_connection_sessions import (
        invalidate_connection_sessions,
    )
    from core.infrastructure.db import project_connection_store

    normalized_auth = normalize_oauth_auth_config(auth_config)
    access_token = _as_text(token_response.get("access_token"))
    if not access_token:
        raise OAuthConnectionError("OAuth token response did not include access_token")
    refresh_token = _as_text(token_response.get("refresh_token")) or _as_text(
        existing_refresh_token
    )
    access_name = _access_token_secret_name(normalized_auth)
    scopes = _granted_scopes(token_response, granted_scope_fallback)
    expires_at = _token_expires_at(token_response, now=now)
    token_type = _as_text(token_response.get("token_type")) or "Bearer"
    secrets_to_save: dict[str, str] = {
        access_name: access_token,
        OAUTH_EXPIRES_AT_SECRET: str(expires_at),
        OAUTH_GRANTED_SCOPES_SECRET: json.dumps(scopes, separators=(",", ":")),
        OAUTH_TOKEN_TYPE_SECRET: token_type,
    }
    if refresh_token:
        secrets_to_save[OAUTH_REFRESH_TOKEN_SECRET] = refresh_token

    connection_id = _as_text(connection.get("connection_id"))
    account_id = _as_text(connection.get("account_id"))
    saved_refs = project_connection_secrets.save_connection_secrets(
        conn,
        account_id=account_id,
        connection_id=connection_id,
        secrets=secrets_to_save,
    )
    secret_refs = _as_dict(connection.get("secret_refs"))
    refs = dict(_as_dict(secret_refs.get("refs")))
    refs.update(_as_dict(saved_refs.get("refs")))
    secret_refs["refs"] = refs

    try:
        current_revision = max(1, int(connection.get("credential_revision") or 1))
    except (TypeError, ValueError):
        current_revision = 1
    updated_config = _as_dict(connection.get("connection_config"))
    oauth_meta = _as_dict(updated_config.get("oauth"))
    oauth_meta.update(
        {
            "lifecycle_status": "connected",
            "connected_at": float(now if now is not None else _now()),
            "expires_at": expires_at,
            "granted_scopes": scopes,
            "token_type": token_type,
            "access_token_secret_name": access_name,
        }
    )
    oauth_meta.update(_as_dict(oauth_metadata))
    updated_config["oauth"] = oauth_meta
    next_revision = current_revision + 1 if bump_credential_revision else current_revision
    project_connection_store.update_project_connection_row(
        conn,
        connection_id,
        {
            "secret_refs": secret_refs,
            "connection_config": updated_config,
            "credential_revision": next_revision,
            "health_status": "healthy",
            "runtime_status": "ready",
            "last_seen_ok_at": float(now if now is not None else _now()),
            "last_error": "",
        },
    )
    if bump_credential_revision:
        invalidate_connection_sessions(conn, connection_id=connection_id)
    updated = project_connection_store.get_project_connection_row(conn, connection_id)
    return updated or {
        **connection,
        "secret_refs": secret_refs,
        "connection_config": updated_config,
    }


def _resolve_oauth_secret_values(conn: Any, connection: dict[str, Any]) -> dict[str, str]:
    from core.application.management.project_connection_secrets import (
        resolve_connection_secrets,
    )

    return resolve_connection_secrets(
        conn,
        account_id=_as_text(connection.get("account_id")),
        secret_refs=connection.get("secret_refs"),
        include_internal=True,
    )


def resolve_oauth_secret_values(conn: Any, connection: dict[str, Any]) -> dict[str, str]:
    """Resolve the host-owned OAuth values attached to one connection."""
    return _resolve_oauth_secret_values(conn, connection)


def _mark_oauth_degraded(conn: Any, connection: dict[str, Any], message: str) -> None:
    from core.infrastructure.db import project_connection_store

    try:
        project_connection_store.update_project_connection_row(
            conn,
            _as_text(connection.get("connection_id")),
            {
                "health_status": "degraded",
                "runtime_status": "degraded",
                "last_error": str(message or "")[:1000],
            },
        )
    except Exception:
        return


def _mark_oauth_provider_unavailable(
    conn: Any,
    connection: dict[str, Any],
    message: str,
) -> None:
    from core.application.management.project_connection_sessions import (
        invalidate_connection_sessions,
    )
    from core.infrastructure.db import project_connection_store

    project_connection_store.update_project_connection_row(
        conn,
        _as_text(connection.get("connection_id")),
        {
            "health_status": "degraded",
            "runtime_status": "unavailable",
            "last_error": str(message or "")[:1000],
        },
    )
    invalidate_connection_sessions(
        conn,
        connection_id=_as_text(connection.get("connection_id")),
    )


def _mark_oauth_reconnect_required(
    conn: Any,
    connection: dict[str, Any],
    *,
    reason: str,
    delete_invalid_refresh_token: bool,
) -> None:
    from core.application.management.project_connection_sessions import (
        invalidate_connection_sessions,
    )
    from core.infrastructure.db import model_provider_store, project_connection_store

    connection_id = _as_text(connection.get("connection_id"))
    account_id = _as_text(connection.get("account_id"))
    secret_refs = _as_dict(connection.get("secret_refs"))
    refs = _as_dict(secret_refs.get("refs"))
    if delete_invalid_refresh_token:
        refresh_ref = _as_text(refs.pop(OAUTH_REFRESH_TOKEN_SECRET, ""))
        if refresh_ref:
            model_provider_store.delete_secret_ref(
                conn,
                account_id=account_id,
                ref_key=refresh_ref,
            )
    secret_refs["refs"] = refs
    config = _as_dict(connection.get("connection_config"))
    oauth = _as_dict(config.get("oauth"))
    oauth["lifecycle_status"] = "reconnect_required"
    oauth["reconnect_reason"] = reason
    config["oauth"] = oauth
    project_connection_store.update_project_connection_row(
        conn,
        connection_id,
        {
            "connection_config": config,
            "secret_refs": secret_refs,
            "health_status": "degraded",
            "runtime_status": "unavailable",
            "last_error": "OAuth authorization requires reconnecting",
        },
    )
    invalidate_connection_sessions(conn, connection_id=connection_id)


def ensure_valid_oauth_access_token(
    conn: Any,
    *,
    connection: dict[str, Any],
    auth_config: dict[str, Any],
    now: float | None = None,
    http_post_form: Callable[..., dict[str, Any]] = _http_post_form,
) -> dict[str, str]:
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store

    normalized_auth = normalize_oauth_auth_config(auth_config)
    access_name = _access_token_secret_name(normalized_auth)
    checked_at = float(now if now is not None else _now())
    values = _resolve_oauth_secret_values(conn, connection)
    access_token = _as_text(values.get(access_name))
    try:
        expires_at = float(values.get(OAUTH_EXPIRES_AT_SECRET) or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if access_token and expires_at > checked_at + OAUTH_REFRESH_SKEW_SECONDS:
        return {access_name: access_token}

    connection_id = _as_text(connection.get("connection_id"))
    lock_name = f"project_connection_oauth_refresh:{connection_id}"
    refresh_error: Exception | None = None
    refresh_connection = connection
    try:
        with project_connection_oauth_store.refresh_advisory_lock(
            conn,
            lock_name=lock_name,
        ) as (lock_acquired, lock_conn):
            if not lock_acquired:
                latest = (
                    project_connection_store.get_project_connection_row(lock_conn, connection_id)
                    or connection
                )
                refresh_connection = latest
                latest_values = _resolve_oauth_secret_values(lock_conn, latest)
                latest_access = _as_text(latest_values.get(access_name))
                try:
                    latest_expires = float(latest_values.get(OAUTH_EXPIRES_AT_SECRET) or 0)
                except (TypeError, ValueError):
                    latest_expires = 0.0
                if latest_access and latest_expires > checked_at:
                    return {access_name: latest_access}
                raise OAuthConnectionError("OAuth token refresh is already in progress")

            try:
                latest = (
                    project_connection_store.get_project_connection_row(lock_conn, connection_id)
                    or connection
                )
                refresh_connection = latest
                latest_values = _resolve_oauth_secret_values(lock_conn, latest)
                latest_access = _as_text(latest_values.get(access_name))
                try:
                    latest_expires = float(latest_values.get(OAUTH_EXPIRES_AT_SECRET) or 0)
                except (TypeError, ValueError):
                    latest_expires = 0.0
                if latest_access and latest_expires > checked_at + OAUTH_REFRESH_SKEW_SECONDS:
                    return {access_name: latest_access}
                refresh_token = _as_text(latest_values.get(OAUTH_REFRESH_TOKEN_SECRET))
                if not refresh_token:
                    raise OAuthConnectionError("OAuth refresh_token is missing; reconnect required")
                attempted_generation = _provider_credential_generation(
                    conn,
                    normalized_auth,
                    connection=latest,
                )
                client_id = _client_credential_value(
                    credential_store=conn,
                    auth_config=normalized_auth,
                    connection=latest,
                    credential_name="client_id",
                    required=True,
                )
                client_secret = _client_credential_value(
                    credential_store=conn,
                    auth_config=normalized_auth,
                    connection=latest,
                    credential_name="client_secret",
                    required=not bool(
                        _as_dict(normalized_auth.get("client_credentials"))
                        .get("client_secret", {})
                        .get("optional", False)
                    ),
                )
                if (
                    _provider_credential_generation(
                        conn,
                        normalized_auth,
                        connection=latest,
                    )
                    != attempted_generation
                ):
                    raise OAuthConnectionError("OAuth provider credentials changed; retry refresh")
                token_response = exchange_refresh_token(
                    auth_config=normalized_auth,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    client_secret=client_secret,
                    http_post_form=http_post_form,
                )
                locked_latest = project_connection_store.get_project_connection_row_for_update(
                    lock_conn,
                    connection_id,
                )
                if not _refresh_response_matches_current_state(
                    conn,
                    attempted_connection=latest,
                    current_connection=locked_latest,
                    auth_config=normalized_auth,
                    attempted_generation=attempted_generation,
                    attempted_client_id=client_id,
                ):
                    raise _OAuthRefreshStaleError(
                        "OAuth refresh response is stale after a connection lifecycle change"
                    )
                locked_connection = _as_dict(locked_latest)
                updated = store_oauth_token_response(
                    lock_conn,
                    connection=locked_connection,
                    auth_config=normalized_auth,
                    token_response=token_response,
                    granted_scope_fallback=_as_string_list(
                        json.loads(latest_values.get(OAUTH_GRANTED_SCOPES_SECRET) or "[]")
                    ),
                    existing_refresh_token=refresh_token,
                    bump_credential_revision=False,
                    now=now,
                )
                updated_values = _resolve_oauth_secret_values(lock_conn, updated)
                refreshed_access = _as_text(updated_values.get(access_name))
                if not refreshed_access:
                    raise OAuthConnectionError("OAuth refresh did not produce an access token")
                return {access_name: refreshed_access}
            except Exception as exc:
                refresh_error = exc
                raise
    except Exception as exc:
        if refresh_error is exc:
            if isinstance(exc, _OAuthRefreshStaleError):
                raise
            invalid_grant = isinstance(exc, OAuthTokenEndpointError) and exc.error_code in {
                "invalid_grant",
                "invalid_token",
            }
            missing_refresh = isinstance(
                exc, OAuthConnectionError
            ) and "refresh_token is missing" in str(exc)
            if invalid_grant or missing_refresh:
                _mark_oauth_reconnect_required(
                    conn,
                    refresh_connection,
                    reason="invalid_grant" if invalid_grant else "missing_refresh_token",
                    delete_invalid_refresh_token=invalid_grant,
                )
            else:
                if isinstance(exc, OAuthTokenEndpointError) and exc.error_code == "invalid_client":
                    _mark_oauth_provider_unavailable(
                        conn,
                        refresh_connection,
                        "OAuth provider configuration is unavailable",
                    )
                else:
                    _mark_oauth_degraded(conn, connection, "OAuth token refresh failed")
            if isinstance(exc, OAuthConnectionError):
                raise
            raise OAuthConnectionError("OAuth token refresh failed") from exc
        raise


def _refresh_response_matches_current_state(
    credential_store: Any,
    *,
    attempted_connection: dict[str, Any],
    current_connection: dict[str, Any] | None,
    auth_config: dict[str, Any],
    attempted_generation: int,
    attempted_client_id: str,
) -> bool:
    current = _as_dict(current_connection)
    if not current or current.get("enabled") is False:
        return False
    for field in (
        "connection_id",
        "account_id",
        "project_id",
        "extension_id",
        "extension_install_id",
        "connection_type",
    ):
        if _as_text(current.get(field)) != _as_text(attempted_connection.get(field)):
            return False
    try:
        attempted_revision = max(1, int(attempted_connection.get("credential_revision") or 1))
        current_revision = max(1, int(current.get("credential_revision") or 1))
    except (TypeError, ValueError):
        return False
    if current_revision != attempted_revision:
        return False
    attempted_oauth = _as_dict(_as_dict(attempted_connection.get("connection_config")).get("oauth"))
    current_oauth = _as_dict(_as_dict(current.get("connection_config")).get("oauth"))
    attempted_lifecycle = _as_text(attempted_oauth.get("lifecycle_status")).lower()
    current_lifecycle = _as_text(current_oauth.get("lifecycle_status")).lower()
    if current_lifecycle != attempted_lifecycle or (
        current_lifecycle and current_lifecycle != "connected"
    ):
        return False
    if _as_text(current.get("runtime_status")).lower() == "unavailable":
        return False
    attempted_refs = _as_dict(_as_dict(attempted_connection.get("secret_refs")).get("refs"))
    current_refs = _as_dict(_as_dict(current.get("secret_refs")).get("refs"))
    if _as_text(current_refs.get(OAUTH_REFRESH_TOKEN_SECRET)) != _as_text(
        attempted_refs.get(OAUTH_REFRESH_TOKEN_SECRET)
    ):
        return False
    if (
        _provider_credential_generation(
            credential_store,
            auth_config,
            connection=current,
        )
        != attempted_generation
    ):
        return False
    try:
        current_client_id = _client_credential_value(
            credential_store=credential_store,
            auth_config=auth_config,
            connection=current,
            credential_name="client_id",
            required=True,
        )
    except OAuthConnectionError:
        return False
    return current_client_id == attempted_client_id


def resolve_oauth_secrets_for_runtime(
    conn: Any,
    *,
    connection: dict[str, Any],
) -> dict[str, str]:
    auth_config = oauth_auth_config_for_connection(conn, connection)
    if not auth_config:
        return {}
    oauth_meta = _as_dict(_as_dict(connection.get("connection_config")).get("oauth"))
    lifecycle = _as_text(oauth_meta.get("lifecycle_status")).lower()
    if lifecycle:
        if lifecycle != "connected":
            raise OAuthConnectionError(f"OAuth connection lifecycle is {lifecycle}")
        if _as_text(connection.get("runtime_status")).lower() == "unavailable":
            raise OAuthConnectionError("OAuth connection runtime is unavailable")
        values = _resolve_oauth_secret_values(conn, connection)
        try:
            granted_scopes = set(
                _as_string_list(json.loads(values.get(OAUTH_GRANTED_SCOPES_SECRET) or "[]"))
            )
        except Exception:
            granted_scopes = set()
        required_scopes = set(_as_string_list(_as_dict(auth_config.get("scopes")).get("required")))
        if not required_scopes.issubset(granted_scopes):
            raise OAuthConnectionError("OAuth connection is missing required scopes")
    return ensure_valid_oauth_access_token(
        conn,
        connection=connection,
        auth_config=auth_config,
    )


def oauth_connection_status(conn: Any, *, connection: dict[str, Any]) -> dict[str, Any]:
    auth_config = oauth_auth_config_for_connection(conn, connection)
    if not auth_config:
        return {
            "oauth": False,
            "status": "not_oauth",
            "connection_id": _as_text(connection.get("connection_id")),
        }
    values = _resolve_oauth_secret_values(conn, connection)
    access_name = _access_token_secret_name(auth_config)
    has_access_token = bool(_as_text(values.get(access_name)))
    has_refresh_token = bool(_as_text(values.get(OAUTH_REFRESH_TOKEN_SECRET)))
    try:
        expires_at = float(values.get(OAUTH_EXPIRES_AT_SECRET) or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    try:
        granted_scopes = json.loads(values.get(OAUTH_GRANTED_SCOPES_SECRET) or "[]")
    except Exception:
        granted_scopes = []
    if not isinstance(granted_scopes, list):
        granted_scopes = []
    oauth_meta = _as_dict(_as_dict(connection.get("connection_config")).get("oauth"))
    lifecycle = _as_text(oauth_meta.get("lifecycle_status")).lower()
    required_scopes = set(_as_string_list(_as_dict(auth_config.get("scopes")).get("required")))
    granted_scope_set = set(_as_string_list(granted_scopes))
    missing_required_scope = not required_scopes.issubset(granted_scope_set)
    if lifecycle:
        status = lifecycle
        if lifecycle == "connected" and (not has_refresh_token or missing_required_scope):
            status = "reconnect_required"
    else:
        status = "not_connected"
        if has_access_token and expires_at > _now():
            status = "connected"
        elif has_access_token:
            status = "expired"
        if _as_text(connection.get("health_status")).lower() == "degraded":
            status = "degraded"
    return {
        "oauth": True,
        "status": status,
        "connection_id": _as_text(connection.get("connection_id")),
        "connection_type": _as_text(connection.get("connection_type")),
        "expires_at": expires_at or None,
        "granted_scopes": _as_string_list(granted_scopes),
        "has_refresh_token": has_refresh_token,
        "access_token_secret_name": access_name,
        "lifecycle_status": lifecycle or None,
        "health_status": _as_text(connection.get("health_status")),
        "runtime_status": _as_text(connection.get("runtime_status")),
        "last_error": _as_text(connection.get("last_error")),
    }
