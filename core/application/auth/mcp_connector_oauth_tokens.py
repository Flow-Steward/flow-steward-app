"""MCP OAuth token exchange, storage and serialized refresh."""

from __future__ import annotations

import json
from typing import Any

from core.application.auth import oauth_client
from core.application.auth.mcp_connector_oauth_policy import (
    _MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT,
    MCP_OAUTH_ACCESS_TOKEN_SECRET,
    MCP_OAUTH_CLIENT_ID_SECRET,
    MCP_OAUTH_EXPIRES_AT_SECRET,
    MCP_OAUTH_GRANTED_SCOPES_SECRET,
    MCP_OAUTH_REFRESH_SKEW_SECONDS,
    MCP_OAUTH_REFRESH_TOKEN_SECRET,
    MCP_OAUTH_TOKEN_TYPE_SECRET,
    _as_dict,
    _as_string_list,
    _as_text,
    _client_secret_for_token,
    _connector_lineage,
    _current_connector_endpoint,
    _is_oauth_binding_error_message,
    _merge_refs,
    _normalized_resource_uri,
    _now,
    _oauth_client_auth,
    _oauth_credential_revision,
    _oauth_metadata,
    _oauth_secret_refs,
    _require_current_oauth_revision,
    _sanitize_oauth_error_message,
    canonical_mcp_resource_uri,
)
from core.application.auth.mcp_connector_oauth_provider import (
    _oauth_callback_url,
    _post_form,
    mcp_client_metadata_document_url,
)
from core.application.auth.mcp_connector_oauth_repository import (
    _latest_connector_row,
    _locked_connector_row,
    _mark_auth_problem,
    _mark_auth_problem_if_current,
    _oauth_metadata_for_update,
    _require_oauth_credentials_bound_to_connector,
    _resolve_connector_oauth_secrets,
    _save_connector_oauth_secrets,
    _update_connector_oauth,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.management.mcp_connector_store import McpConnectorStore
from core.infrastructure.db import mcp_connector_oauth_store
from core.infrastructure.db.db_pool import (
    transaction_connection,
)


def _exchange_token(
    *,
    auth_config: dict[str, Any],
    data: dict[str, str],
    client_id: str,
    client_secret: str,
    post_form: Any = _post_form,
) -> dict[str, Any]:
    method = _as_text(auth_config.get("token_endpoint_auth_method")).lower() or "none"
    payload, basic_auth = _oauth_client_auth(
        data,
        method=method,
        client_id=client_id,
        client_secret=client_secret,
    )
    payload["resource"] = _as_text(auth_config.get("resource"))
    return post_form(
        _as_text(auth_config.get("token_endpoint")),
        payload,
        purpose="MCP OAuth token endpoint",
        basic_auth=basic_auth,
    )


def _store_token_response(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    auth_config: dict[str, Any],
    token_response: dict[str, Any],
    granted_scope_fallback: list[str],
    existing_refresh_token: str = "",
    expected_credential_revision: int,
    now: float | None = None,
) -> dict[str, Any]:
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, connector_row)
        try:
            current_revision = max(1, int(locked_row.get("oauth_credential_revision") or 1))
        except (TypeError, ValueError):
            current_revision = 1
        if current_revision != max(1, int(expected_credential_revision)):
            raise OAuthConnectionError(
                "MCP OAuth credential revision changed during token exchange; reconnect required"
            )
        updated = _store_token_response_locked(
            transaction_conn,
            connector_row=locked_row,
            auth_config=auth_config,
            token_response=token_response,
            granted_scope_fallback=granted_scope_fallback,
            existing_refresh_token=existing_refresh_token,
            now=now,
        )
    deferred_error = _as_text(updated.get("_oauth_deferred_error"))
    if deferred_error:
        raise OAuthConnectionError(deferred_error)
    return updated


def _store_token_response_locked(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    auth_config: dict[str, Any],
    token_response: dict[str, Any],
    granted_scope_fallback: list[str],
    existing_refresh_token: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    access_token = _as_text(token_response.get("access_token"))
    if not access_token:
        raise OAuthConnectionError("MCP OAuth token response did not include access_token")
    refresh_token = _as_text(token_response.get("refresh_token")) or existing_refresh_token
    scopes = oauth_client._granted_scopes(token_response, granted_scope_fallback)
    expires_at = oauth_client._token_expires_at(token_response, now=now)
    token_type = _as_text(token_response.get("token_type")) or "Bearer"
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    latest_row = _latest_connector_row(conn, connector_row)
    metadata = _oauth_metadata_for_update(conn, latest_row)
    if bool(metadata.get("refresh_token_required")) and not refresh_token:
        message = "MCP OAuth token response did not include refresh_token; reconnect required"
        _mark_auth_problem(
            conn,
            latest_row,
            auth_status="auth_required",
            message=message,
        )
        return {**latest_row, "_oauth_deferred_error": message}
    expected_endpoint = _as_text(auth_config.get("connector_endpoint"))
    if expected_endpoint and _as_text(latest_row.get("auth_mode")).lower() != "oauth2":
        raise OAuthConnectionError(
            "MCP connector auth mode changed during OAuth authorization; reconnect required"
        )
    if expected_endpoint and _normalized_resource_uri(
        expected_endpoint
    ) != _current_connector_endpoint(latest_row):
        raise OAuthConnectionError(
            _MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT
        )
    saved_refs = _save_connector_oauth_secrets(
        conn,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
        connector_id=connector_id,
        secrets_to_save={
            MCP_OAUTH_ACCESS_TOKEN_SECRET: access_token,
            MCP_OAUTH_REFRESH_TOKEN_SECRET: refresh_token,
            MCP_OAUTH_EXPIRES_AT_SECRET: str(expires_at),
            MCP_OAUTH_GRANTED_SCOPES_SECRET: json.dumps(scopes, separators=(",", ":")),
            MCP_OAUTH_TOKEN_TYPE_SECRET: token_type,
        },
    )
    secret_refs = _merge_refs(_oauth_secret_refs(latest_row), saved_refs)
    token_meta = _as_dict(metadata.get("token"))
    token_meta.update(
        {
            "connected_at": token_meta.get("connected_at") or float(now or _now()),
            "last_refresh_at": float(now or _now()),
            "expires_at": expires_at,
            "granted_scopes": scopes,
            "token_type": token_type,
            "connector_endpoint": expected_endpoint or _current_connector_endpoint(latest_row),
            "resource": _as_text(auth_config.get("resource")),
            "issuer": _as_text(auth_config.get("issuer")),
            "registration_mode": _as_text(_as_dict(metadata.get("registration")).get("mode")),
        }
    )
    metadata["token"] = token_meta
    _update_connector_oauth(
        conn,
        connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
        updates={
            "oauth_secret_refs_jsonb": secret_refs,
            "oauth_metadata_jsonb": metadata,
            "auth_status": "connected",
            "status": "active",
            "oauth_last_error": "",
        },
    )
    updated = McpConnectorStore().get_connector_row(
        conn,
        connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )
    connector_row["oauth_secret_refs_jsonb"] = secret_refs
    connector_row["oauth_metadata_jsonb"] = metadata
    connector_row["auth_status"] = "connected"
    return updated or {
        **latest_row,
        "oauth_secret_refs_jsonb": secret_refs,
        "oauth_metadata_jsonb": metadata,
        "auth_status": "connected",
    }


def ensure_valid_mcp_connector_access_token(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    force_refresh: bool = False,
    now: float | None = None,
    post_form: Any = _post_form,
) -> str:
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    checked_at = float(now if now is not None else _now())
    values = _resolve_connector_oauth_secrets(conn, connector_row)
    access_token = _as_text(values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET))
    metadata = _oauth_metadata(connector_row)
    _require_oauth_credentials_bound_to_connector(
        conn,
        connector_row,
        metadata=metadata,
        values=values,
    )
    try:
        expires_at = float(values.get(MCP_OAUTH_EXPIRES_AT_SECRET) or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if (
        not force_refresh
        and access_token
        and expires_at > checked_at + MCP_OAUTH_REFRESH_SKEW_SECONDS
    ):
        return access_token

    lock_name = f"mcp_connector_oauth_refresh:{account_id}:{connector_id}"
    refresh_error: Exception | None = None
    try:
        with mcp_connector_oauth_store.refresh_advisory_lock(
            conn,
            lock_name=lock_name,
        ) as (lock_acquired, lock_conn):
            if not lock_acquired:
                latest = (
                    McpConnectorStore().get_connector_row(
                        lock_conn,
                        connector_id,
                        account_id=account_id,
                        project_id=project_id,
                        tenant_id=tenant_id,
                    )
                    or connector_row
                )
                _require_current_oauth_revision(connector_row, latest)
                latest_values = _resolve_connector_oauth_secrets(lock_conn, latest)
                latest_metadata = _oauth_metadata(latest)
                _require_oauth_credentials_bound_to_connector(
                    lock_conn,
                    latest,
                    metadata=latest_metadata,
                    values=latest_values,
                )
                latest_access = _as_text(latest_values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET))
                try:
                    latest_expires = float(latest_values.get(MCP_OAUTH_EXPIRES_AT_SECRET) or 0)
                except (TypeError, ValueError):
                    latest_expires = 0.0
                if latest_access and latest_expires > checked_at:
                    return latest_access
                raise OAuthConnectionError("MCP OAuth token refresh is already in progress")

            try:
                with transaction_connection(lock_conn) as transaction_conn:
                    latest = _locked_connector_row(transaction_conn, connector_row)
                    _require_current_oauth_revision(connector_row, latest)
                    latest_values = _resolve_connector_oauth_secrets(transaction_conn, latest)
                    latest_metadata = _oauth_metadata(latest)
                    _require_oauth_credentials_bound_to_connector(
                        transaction_conn,
                        latest,
                        metadata=latest_metadata,
                        values=latest_values,
                    )
                    latest_access = _as_text(latest_values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET))
                    try:
                        latest_expires = float(latest_values.get(MCP_OAUTH_EXPIRES_AT_SECRET) or 0)
                    except (TypeError, ValueError):
                        latest_expires = 0.0
                    if (
                        not force_refresh
                        and latest_access
                        and latest_expires > checked_at + MCP_OAUTH_REFRESH_SKEW_SECONDS
                    ):
                        return latest_access
                    refresh_token = _as_text(latest_values.get(MCP_OAUTH_REFRESH_TOKEN_SECRET))
                    if not refresh_token:
                        raise OAuthConnectionError(
                            "MCP OAuth refresh_token is missing; reconnect required"
                        )
                    discovery = _as_dict(latest_metadata.get("discovery"))
                    server = _as_dict(discovery.get("authorization_server"))
                    registration = _as_dict(latest_metadata.get("registration"))
                    if not server:
                        raise OAuthConnectionError(
                            "MCP OAuth discovery metadata is missing; reconnect required"
                        )
                    auth_config = {
                        "token_endpoint": _as_text(server.get("token_endpoint")),
                        "connector_endpoint": _current_connector_endpoint(latest),
                        "resource": _as_text(discovery.get("resource"))
                        or canonical_mcp_resource_uri(_as_text(latest.get("base_url"))),
                        "issuer": _as_text(discovery.get("issuer")),
                        "token_endpoint_auth_method": _as_text(
                            registration.get("token_endpoint_auth_method")
                        )
                        or "none",
                    }
                    client_id = _as_text(latest_values.get(MCP_OAUTH_CLIENT_ID_SECRET))
                    if not client_id and registration.get("mode") == "client_id_metadata_document":
                        client_id = mcp_client_metadata_document_url(connector_id)
                    if not client_id:
                        raise OAuthConnectionError(
                            "MCP OAuth client_id is missing; reconnect required"
                        )
                    client_secret = _client_secret_for_token(latest_values, auth_config=auth_config)
                    token_response = _exchange_token(
                        auth_config=auth_config,
                        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                        client_id=client_id,
                        client_secret=client_secret,
                        post_form=post_form,
                    )
                    _require_current_oauth_revision(
                        latest, _latest_connector_row(transaction_conn, latest)
                    )
                    updated = _store_token_response_locked(
                        transaction_conn,
                        connector_row=latest,
                        auth_config=auth_config,
                        token_response=token_response,
                        granted_scope_fallback=_as_string_list(
                            json.loads(latest_values.get(MCP_OAUTH_GRANTED_SCOPES_SECRET) or "[]")
                        ),
                        existing_refresh_token=refresh_token,
                        now=now,
                    )
                deferred_error = _as_text(updated.get("_oauth_deferred_error"))
                if deferred_error:
                    raise OAuthConnectionError(deferred_error)
                updated_values = _resolve_connector_oauth_secrets(lock_conn, updated)
                refreshed_access = _as_text(updated_values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET))
                if not refreshed_access:
                    raise OAuthConnectionError("MCP OAuth refresh did not produce an access token")
                return refreshed_access
            except Exception as exc:
                refresh_error = exc
                raise
    except Exception as exc:
        if refresh_error is exc:
            if isinstance(exc, OAuthConnectionError) and _is_oauth_binding_error_message(str(exc)):
                raise
            safe_message = _sanitize_oauth_error_message(f"MCP OAuth token refresh failed: {exc}")
            failure_status = (
                "auth_required" if "refresh_token is missing" in str(exc) else "refresh_failed"
            )
            _mark_auth_problem_if_current(
                conn,
                connector_row,
                expected_credential_revision=_oauth_credential_revision(connector_row),
                auth_status=failure_status,
                message=safe_message,
            )
            raise OAuthConnectionError(safe_message) from exc
        raise


def mcp_connector_oauth_status(conn: Any, *, connector_row: dict[str, Any]) -> dict[str, Any]:
    metadata = _oauth_metadata(connector_row)
    discovery = _as_dict(metadata.get("discovery"))
    registration = _as_dict(metadata.get("registration"))
    token = _as_dict(metadata.get("token"))
    values = _resolve_connector_oauth_secrets(conn, connector_row)
    has_access = bool(_as_text(values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET)))
    has_refresh = bool(_as_text(values.get(MCP_OAUTH_REFRESH_TOKEN_SECRET)))
    return {
        "status": _as_text(connector_row.get("auth_status")) or "not_configured",
        "redirect_uri": _oauth_callback_url(),
        "issuer": _as_text(discovery.get("issuer")),
        "registration_mode": _as_text(registration.get("mode")),
        "resource": _as_text(discovery.get("resource")),
        "expires_at": token.get("expires_at"),
        "granted_scopes": _as_string_list(token.get("granted_scopes")),
        "connected_identity": _as_text(connector_row.get("oauth_connected_identity")),
        "has_access_token": has_access,
        "has_refresh_token": has_refresh,
        "last_error": _as_text(connector_row.get("oauth_last_error")),
    }
