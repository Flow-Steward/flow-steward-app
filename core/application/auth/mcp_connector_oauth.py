"""MCP OAuth authorization, callback transactions and connector credential lifecycle."""

from __future__ import annotations

import contextlib
import secrets
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from core.application.auth import oauth_client
from core.application.auth.mcp_connector_oauth_policy import (
    _MCP_OAUTH_APPLICATION_SECRET_NAMES,
    _MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT,
    _MESSAGE_OAUTH_STATE_DOES_NOT_MATCH_THE_STORED_MCP_CONNECTOR,
    _MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED,
    _MESSAGE_OAUTH_STATE_IS_NOT_FOR_AN_MCP_CONNECTOR,
    _as_dict,
    _as_string_list,
    _as_text,
    _client_secret_for_token,
    _connector_lineage,
    _current_connector_endpoint,
    _issuer_identifiers_match,
    _manual_token_endpoint_auth_method,
    _merge_refs,
    _normalized_resource_uri,
    _now,
    _oauth_binding_error,
    _oauth_client_auth,
    _oauth_credential_revision,
    _oauth_metadata,
    _refs_map,
    _registration_issuer_binding_error,
    _require_current_oauth_revision,
    _sanitize_oauth_error_message,
    _scopes_for_start,
    _validate_metadata_url,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_ACCESS_TOKEN_SECRET as MCP_OAUTH_ACCESS_TOKEN_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_CLIENT_ID_SECRET as MCP_OAUTH_CLIENT_ID_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_CLIENT_SECRET_SECRET as MCP_OAUTH_CLIENT_SECRET_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_EXPIRES_AT_SECRET as MCP_OAUTH_EXPIRES_AT_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_GRANTED_SCOPES_SECRET as MCP_OAUTH_GRANTED_SCOPES_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_REFRESH_SKEW_SECONDS as MCP_OAUTH_REFRESH_SKEW_SECONDS,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_REFRESH_TOKEN_SECRET as MCP_OAUTH_REFRESH_TOKEN_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET as MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_STATE_TYPE as MCP_OAUTH_STATE_TYPE,
)
from core.application.auth.mcp_connector_oauth_policy import (
    MCP_OAUTH_TOKEN_TYPE_SECRET as MCP_OAUTH_TOKEN_TYPE_SECRET,
)
from core.application.auth.mcp_connector_oauth_policy import (
    McpOAuthManualCredentialsRequired as McpOAuthManualCredentialsRequired,
)
from core.application.auth.mcp_connector_oauth_policy import (
    authorization_server_metadata_urls as authorization_server_metadata_urls,
)
from core.application.auth.mcp_connector_oauth_policy import (
    canonical_mcp_resource_uri as canonical_mcp_resource_uri,
)
from core.application.auth.mcp_connector_oauth_policy import (
    parse_www_authenticate as parse_www_authenticate,
)
from core.application.auth.mcp_connector_oauth_policy import (
    protected_resource_metadata_urls as protected_resource_metadata_urls,
)
from core.application.auth.mcp_connector_oauth_provider import (
    _fetch_json,
    _oauth_callback_url,
    _post_form,
    _post_json,
    _register_client,
    _selected_discovery,
)
from core.application.auth.mcp_connector_oauth_provider import (
    discover_mcp_oauth as discover_mcp_oauth,
)
from core.application.auth.mcp_connector_oauth_provider import (
    mcp_client_metadata_document as mcp_client_metadata_document,
)
from core.application.auth.mcp_connector_oauth_provider import (
    mcp_client_metadata_document_url as mcp_client_metadata_document_url,
)
from core.application.auth.mcp_connector_oauth_repository import (
    _latest_connector_row,
    _locked_connector_row,
    _mark_auth_problem,
    _mark_auth_problem_if_current,
    _oauth_metadata_for_update,
    _resolve_connector_oauth_secrets,
    _save_connector_oauth_secrets,
    _update_connector_oauth,
)
from core.application.auth.mcp_connector_oauth_rpc import (
    _probe_mcp_oauth_www_authenticate,
)
from core.application.auth.mcp_connector_oauth_rpc import (
    mcp_connector_rpc_context as mcp_connector_rpc_context,
)
from core.application.auth.mcp_connector_oauth_rpc import (
    mcp_connector_rpc_post as mcp_connector_rpc_post,
)
from core.application.auth.mcp_connector_oauth_rpc import (
    mcp_connector_rpc_post_with_latency as mcp_connector_rpc_post_with_latency,
)
from core.application.auth.mcp_connector_oauth_tokens import (
    _exchange_token,
    _store_token_response_locked,
)
from core.application.auth.mcp_connector_oauth_tokens import (
    ensure_valid_mcp_connector_access_token as ensure_valid_mcp_connector_access_token,
)
from core.application.auth.mcp_connector_oauth_tokens import (
    mcp_connector_oauth_status as mcp_connector_oauth_status,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.application.management.mcp_connector_header_secrets import (
    resolve_connector_headers,
)
from core.infrastructure.db import mcp_connector_oauth_store, model_provider_store
from core.infrastructure.db.db_pool import (
    register_transaction_after_commit,
    transaction_connection,
)


def mcp_connector_oauth_reset_updates(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    auth_status: str,
    message: str = "",
    preserve_client_credentials: bool = False,
) -> dict[str, Any]:
    _connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    preserved_names = {
        MCP_OAUTH_CLIENT_ID_SECRET,
        MCP_OAUTH_CLIENT_SECRET_SECRET,
        MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    }
    current_metadata = _oauth_metadata_for_update(conn, connector_row)
    registration_mode = _as_text(_as_dict(current_metadata.get("registration")).get("mode")).lower()
    preserve_manual_credentials = preserve_client_credentials and registration_mode == "manual"
    remaining_refs: dict[str, str] = {}
    for name, ref_key in _refs_map(connector_row).items():
        if preserve_manual_credentials and name in preserved_names:
            remaining_refs[name] = ref_key
            continue
        model_provider_store.delete_secret_ref(
            conn,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            ref_key=ref_key,
        )
    mcp_connector_oauth_store.delete_connector_oauth_states(
        conn,
        connector_id=_connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )
    try:
        current_revision = max(1, int(connector_row.get("oauth_credential_revision") or 1))
    except (TypeError, ValueError):
        current_revision = 1
    if preserve_client_credentials:
        retained_metadata = {
            key: value
            for key, value in current_metadata.items()
            if key != "token" and (preserve_manual_credentials or key != "registration")
        }
    else:
        retained_metadata = {
            key: current_metadata[key]
            for key in (
                "requested_scopes",
                "authorization_parameters",
                "refresh_token_required",
            )
            if key in current_metadata
        }
    return {
        "oauth_secret_refs_jsonb": {"refs": remaining_refs} if remaining_refs else {},
        "oauth_metadata_jsonb": retained_metadata,
        "oauth_credential_revision": current_revision + 1,
        "oauth_connected_identity": "",
        "oauth_last_error": _sanitize_oauth_error_message(message),
        "auth_status": auth_status,
    }


def save_mcp_connector_oauth_credentials(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Store operator-supplied client credentials; values are never returned."""
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, connector_row)
        _require_current_oauth_revision(connector_row, locked_row)
        return _save_mcp_connector_oauth_credentials(
            transaction_conn,
            connector_row=locked_row,
            client_id=client_id,
            client_secret=client_secret,
        )


def _save_mcp_connector_oauth_credentials(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    normalized_id = _as_text(client_id)
    normalized_secret = client_secret if isinstance(client_secret, str) else ""
    if not normalized_id or not normalized_secret:
        raise OAuthConnectionError("Client ID and Client Secret are required")
    existing_metadata = _oauth_metadata_for_update(conn, connector_row)
    had_authorized_account = bool(
        _as_text(connector_row.get("oauth_connected_identity"))
        or _as_dict(existing_metadata.get("token"))
        or any(name not in _MCP_OAUTH_APPLICATION_SECRET_NAMES for name in _refs_map(connector_row))
    )
    metadata = {key: value for key, value in existing_metadata.items() if key != "token"}
    discovery = _as_dict(metadata.get("discovery"))
    server = _as_dict(discovery.get("authorization_server"))
    token_endpoint_auth_method = _manual_token_endpoint_auth_method(
        server,
        has_client_secret=bool(normalized_secret),
    )
    metadata["registration"] = {
        "mode": "manual",
        "issuer": _as_text(discovery.get("issuer")),
        "token_endpoint_auth_method": token_endpoint_auth_method,
    }
    prepared_revocation = (
        _prepare_mcp_connector_grant_revocation(conn, connector_row=connector_row)
        if had_authorized_account
        else None
    )
    reset = mcp_connector_oauth_reset_updates(
        conn,
        connector_row=connector_row,
        auth_status="auth_required",
        message=(
            "MCP OAuth client credentials changed; reconnect required"
            if had_authorized_account
            else ""
        ),
        preserve_client_credentials=False,
    )
    saved = _save_connector_oauth_secrets(
        conn,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
        connector_id=connector_id,
        secrets_to_save={
            MCP_OAUTH_CLIENT_ID_SECRET: normalized_id,
            MCP_OAUTH_CLIENT_SECRET_SECRET: normalized_secret,
        },
    )
    refs = _merge_refs(reset.get("oauth_secret_refs_jsonb", {}), saved)
    updates = {
        **reset,
        "oauth_secret_refs_jsonb": refs,
        "oauth_metadata_jsonb": metadata,
    }
    _update_connector_oauth(
        conn,
        connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
        updates=updates,
    )
    schedule_prepared_mcp_connector_revocation(conn, prepared_revocation)
    return {"saved": True, "has_client_id": True, "has_client_secret": True}


def start_mcp_connector_oauth(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    return_path: str = "",
    now: float | None = None,
    fetch_json: Any = _fetch_json,
    post_json: Any = _post_json,
) -> dict[str, Any]:
    if _as_text(connector_row.get("auth_mode")).lower() != "oauth2":
        raise OAuthConnectionError("MCP connector auth_mode must be oauth2")
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    expected_revision = _oauth_credential_revision(connector_row)
    with contextlib.suppress(Exception):
        mcp_connector_oauth_store.delete_expired_oauth_states(conn)

    cached_discovery = _as_dict(_oauth_metadata(connector_row).get("discovery"))
    if _as_dict(cached_discovery.get("authorization_server")):
        discovery = _selected_discovery(conn, connector_row)
    else:
        discovery = discover_mcp_oauth(
            conn,
            connector_row=connector_row,
            www_authenticate=_probe_mcp_oauth_www_authenticate(
                {**connector_row, "headers": resolve_connector_headers(conn, connector_row)}
            ),
            fetch_json=fetch_json,
            expected_credential_revision=expected_revision,
        )
    connector_metadata = _oauth_metadata_for_update(conn, connector_row)
    scopes = _scopes_for_start(
        discovery,
        _as_string_list(connector_metadata.get("requested_scopes")),
    )
    registration = _register_client(
        conn,
        connector_row=connector_row,
        discovery=discovery,
        scopes=scopes,
        post_json=post_json,
        expected_credential_revision=expected_revision,
    )
    server = _as_dict(discovery.get("authorization_server"))
    resource = _as_text(discovery.get("resource")) or canonical_mcp_resource_uri(
        _as_text(connector_row.get("base_url"))
    )
    connector_endpoint = _current_connector_endpoint(connector_row)
    started_at = float(now if now is not None else _now())
    expires_at = started_at + oauth_client.OAUTH_STATE_TTL_SECONDS
    verifier = oauth_client._code_verifier()
    redirect_uri = _oauth_callback_url()
    fallback_path = f"/registry/mcp/{quote(connector_id)}?project_id={quote(project_id)}"
    safe_return_path = oauth_client.sanitize_oauth_return_path(
        return_path,
        fallback=fallback_path,
    )
    state_payload = {
        "v": 1,
        "type": MCP_OAUTH_STATE_TYPE,
        "nonce": secrets.token_urlsafe(18),
        "connector_id": connector_id,
        "account_id": account_id,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "issuer": _as_text(discovery.get("issuer")),
        "registration_mode": registration["mode"],
        "connector_endpoint": connector_endpoint,
        "resource": resource,
        "exp": int(expires_at),
    }
    auth_config = {
        "authorization_endpoint": _as_text(server.get("authorization_endpoint")),
        "token_endpoint": _as_text(server.get("token_endpoint")),
        "revocation_endpoint": _as_text(server.get("revocation_endpoint")),
        "connector_endpoint": connector_endpoint,
        "resource": resource,
        "issuer": _as_text(discovery.get("issuer")),
        "token_endpoint_auth_method": registration["token_endpoint_auth_method"],
    }
    expected_row = {**connector_row, "oauth_credential_revision": expected_revision}
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, expected_row)
        _require_current_oauth_revision(expected_row, locked_row)
        authorization_revision = expected_revision + 1
        _update_connector_oauth(
            transaction_conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            updates={"oauth_credential_revision": authorization_revision},
        )
        if callable(getattr(transaction_conn, "cursor", None)):
            mcp_connector_oauth_store.delete_connector_oauth_states(
                transaction_conn,
                connector_id=connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )
        state_payload["oauth_credential_revision"] = authorization_revision
        state = oauth_client._build_state_token(state_payload)
        mcp_connector_oauth_store.insert_oauth_state(
            transaction_conn,
            state_hash=oauth_client._state_hash(state),
            connector_id=connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            oauth_credential_revision=authorization_revision,
            code_verifier_encrypted=encrypt_secret(verifier),
            redirect_uri=redirect_uri,
            return_path=safe_return_path,
            auth_payload={
                "auth_config": auth_config,
                "client_id": registration["client_id"],
                "connector_endpoint": connector_endpoint,
                "scopes": scopes,
            },
            expires_at=expires_at,
        )
    auth_params = {
        **{
            str(key): str(value)
            for key, value in _as_dict(connector_metadata.get("authorization_parameters")).items()
        },
        "response_type": "code",
        "client_id": registration["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": oauth_client._code_challenge(verifier),
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if scopes:
        auth_params["scope"] = " ".join(scopes)
    authorization_endpoint = urlsplit(_as_text(server.get("authorization_endpoint")))
    if authorization_endpoint.fragment:
        raise OAuthConnectionError("MCP OAuth authorization endpoint must not contain a fragment")
    protected_keys = set(auth_params)
    existing_query = [
        (key, value)
        for key, value in parse_qsl(authorization_endpoint.query, keep_blank_values=True)
        if key not in protected_keys
    ]
    authorization_url = urlunsplit(
        (
            authorization_endpoint.scheme,
            authorization_endpoint.netloc,
            authorization_endpoint.path,
            urlencode([*existing_query, *auth_params.items()]),
            "",
        )
    )
    _validate_metadata_url(authorization_url, purpose="MCP OAuth authorization URL")
    return {
        "authorization_url": authorization_url,
        "connector_id": connector_id,
        "expires_at": expires_at,
        "scopes": scopes,
        "issuer": _as_text(discovery.get("issuer")),
        "registration_mode": registration["mode"],
        "redirect_uri": redirect_uri,
        "resource": resource,
    }


def _verify_oauth_callback_connector_binding(
    conn: Any,
    connector_row: dict[str, Any],
    *,
    auth_config: dict[str, Any],
    state_payload: dict[str, Any],
) -> None:
    expected_endpoint = _as_text(auth_config.get("connector_endpoint")) or _as_text(
        state_payload.get("connector_endpoint")
    )
    expected_resource = _as_text(auth_config.get("resource")) or _as_text(
        state_payload.get("resource")
    )
    expected_issuer = _as_text(auth_config.get("issuer")) or _as_text(state_payload.get("issuer"))
    current_auth_mode = _as_text(connector_row.get("auth_mode")).lower()
    message = ""
    if current_auth_mode != "oauth2":
        message = "MCP connector auth mode changed during OAuth authorization; reconnect required"
    elif not expected_endpoint:
        message = "MCP OAuth state is missing connector endpoint; reconnect required"
    elif _normalized_resource_uri(expected_endpoint) != _current_connector_endpoint(connector_row):
        message = _MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT
    else:
        discovery = _as_dict(_oauth_metadata(connector_row).get("discovery"))
        discovery_endpoint = _as_text(discovery.get("connector_endpoint"))
        discovery_resource = _as_text(discovery.get("resource"))
        discovery_issuer = _as_text(discovery.get("issuer"))
        if not discovery:
            message = "MCP OAuth connector authorization was reset; reconnect required"
        elif discovery_endpoint and _normalized_resource_uri(
            discovery_endpoint
        ) != _normalized_resource_uri(expected_endpoint):
            message = _MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT
        elif (
            expected_resource
            and discovery_resource
            and _normalized_resource_uri(expected_resource)
            != _normalized_resource_uri(discovery_resource)
        ):
            message = (
                "MCP OAuth protected resource changed during authorization; reconnect required"
            )
        elif (
            expected_issuer
            and discovery_issuer
            and not _issuer_identifiers_match(expected_issuer, discovery_issuer)
        ):
            message = (
                "MCP OAuth authorization server changed during authorization; reconnect required"
            )
    if not message:
        return
    _mark_auth_problem(
        conn,
        connector_row,
        auth_status="auth_required" if current_auth_mode == "oauth2" else "not_configured",
        message=message,
    )
    raise OAuthConnectionError(message)


def handle_mcp_oauth_callback(
    conn: Any,
    *,
    state: str,
    code: str,
    now: float | None = None,
    post_form: Any = _post_form,
) -> dict[str, Any]:
    code_text = _as_text(code)
    if not code_text:
        raise OAuthConnectionError("MCP OAuth callback code is required")
    state_payload = oauth_client._verify_state_token(state, now=now)
    if _as_text(state_payload.get("type")) != MCP_OAUTH_STATE_TYPE:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_IS_NOT_FOR_AN_MCP_CONNECTOR)
    with contextlib.suppress(Exception):
        mcp_connector_oauth_store.delete_expired_oauth_states(conn)
    row = mcp_connector_oauth_store.consume_oauth_state(
        conn,
        state_hash=oauth_client._state_hash(state),
    )
    if not row:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
    for key in (
        "connector_id",
        "account_id",
        "project_id",
        "tenant_id",
        "oauth_credential_revision",
    ):
        if _as_text(row.get(key)) != _as_text(state_payload.get(key)):
            raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_DOES_NOT_MATCH_THE_STORED_MCP_CONNECTOR)
    with transaction_connection(conn) as transaction_conn:
        connector = _locked_connector_row(transaction_conn, row)
        _require_current_oauth_revision(row, connector)
        auth_payload = _as_dict(row.get("auth_payload_jsonb"))
        auth_config = _as_dict(auth_payload.get("auth_config"))
        _verify_oauth_callback_connector_binding(
            transaction_conn,
            connector,
            auth_config=auth_config,
            state_payload=state_payload,
        )
        registration_problem = _registration_issuer_binding_error(
            _as_dict(_oauth_metadata(connector).get("registration")),
            discovery={
                "issuer": _as_text(auth_config.get("issuer"))
                or _as_text(state_payload.get("issuer"))
            },
        )
        if registration_problem:
            _mark_auth_problem(
                transaction_conn,
                connector,
                auth_status="auth_required",
                message=registration_problem,
            )
            raise OAuthConnectionError(registration_problem)
        values = _resolve_connector_oauth_secrets(transaction_conn, connector)
        client_id = _as_text(auth_payload.get("client_id")) or _as_text(
            values.get(MCP_OAUTH_CLIENT_ID_SECRET)
        )
        if not client_id:
            raise OAuthConnectionError("MCP OAuth client_id is missing")
        client_secret = _client_secret_for_token(values, auth_config=auth_config)
        verifier = decrypt_secret(_as_text(row.get("code_verifier_encrypted")))
        token_response = _exchange_token(
            auth_config=auth_config,
            data={
                "grant_type": "authorization_code",
                "code": code_text,
                "redirect_uri": _as_text(row.get("redirect_uri")),
                "code_verifier": verifier,
            },
            client_id=client_id,
            client_secret=client_secret,
            post_form=post_form,
        )
        _require_current_oauth_revision(
            connector, _latest_connector_row(transaction_conn, connector)
        )
        updated = _store_token_response_locked(
            transaction_conn,
            connector_row=connector,
            auth_config=auth_config,
            token_response=token_response,
            granted_scope_fallback=_as_string_list(auth_payload.get("scopes")),
            now=now,
        )
    deferred_error = _as_text(updated.get("_oauth_deferred_error"))
    if deferred_error:
        raise OAuthConnectionError(deferred_error)
    return {
        "status": "connected",
        "connector_id": _as_text(updated.get("connector_id")),
        "return_path": _as_text(row.get("return_path")),
    }


def mcp_oauth_callback_context(
    conn: Any,
    *,
    state: str,
    fallback_return_path: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve safe MCP callback routing before the one-time state is consumed."""
    state_payload = oauth_client._verify_state_token(state, now=now)
    if _as_text(state_payload.get("type")) != MCP_OAUTH_STATE_TYPE:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_IS_NOT_FOR_AN_MCP_CONNECTOR)
    row = mcp_connector_oauth_store.get_oauth_state(
        conn,
        state_hash=oauth_client._state_hash(state),
    )
    if not row:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
    for key in (
        "connector_id",
        "account_id",
        "project_id",
        "tenant_id",
        "oauth_credential_revision",
    ):
        if _as_text(row.get(key)) != _as_text(state_payload.get(key)):
            raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_DOES_NOT_MATCH_THE_STORED_MCP_CONNECTOR)
    return {
        "return_path": oauth_client.sanitize_oauth_return_path(
            _as_text(row.get("return_path")),
            fallback=fallback_return_path,
        ),
        "connector_id": _as_text(row.get("connector_id")),
        "account_id": _as_text(row.get("account_id")),
        "project_id": _as_text(row.get("project_id")),
        "tenant_id": _as_text(row.get("tenant_id")),
    }


def handle_mcp_oauth_provider_error(
    conn: Any,
    *,
    state: str,
    fallback_return_path: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Consume a rejected MCP authorization without retaining provider details."""
    state_payload = oauth_client._verify_state_token(state, now=now)
    if _as_text(state_payload.get("type")) != MCP_OAUTH_STATE_TYPE:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_IS_NOT_FOR_AN_MCP_CONNECTOR)
    row = mcp_connector_oauth_store.consume_oauth_state(
        conn,
        state_hash=oauth_client._state_hash(state),
    )
    if not row:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
    for key in (
        "connector_id",
        "account_id",
        "project_id",
        "tenant_id",
        "oauth_credential_revision",
    ):
        if _as_text(row.get(key)) != _as_text(state_payload.get(key)):
            raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_DOES_NOT_MATCH_THE_STORED_MCP_CONNECTOR)
    message = "OAuth authorization was denied or failed; start authorization again."
    _mark_auth_problem_if_current(
        conn,
        row,
        expected_credential_revision=_oauth_credential_revision(row),
        auth_status="auth_required",
        message=message,
    )
    return {
        "status": "auth_required",
        "connector_id": _as_text(row.get("connector_id")),
        "return_path": oauth_client.sanitize_oauth_return_path(
            _as_text(row.get("return_path")),
            fallback=fallback_return_path,
        ),
    }


def _best_effort_revoke_mcp_connector_grant(
    conn: Any,
    *,
    connector_row: dict[str, Any],
) -> None:
    prepared = _prepare_mcp_connector_grant_revocation(conn, connector_row=connector_row)
    execute_prepared_mcp_connector_revocation(prepared)


def _prepare_mcp_connector_grant_revocation(
    conn: Any,
    *,
    connector_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve a revocation request while the connector and its secrets still exist."""
    metadata = _oauth_metadata(connector_row)
    discovery = _as_dict(metadata.get("discovery"))
    server = _as_dict(discovery.get("authorization_server"))
    try:
        values = _resolve_connector_oauth_secrets(conn, connector_row)
    except Exception:
        return None
    token = _as_text(values.get(MCP_OAUTH_REFRESH_TOKEN_SECRET)) or _as_text(
        values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET)
    )
    revocation_endpoint = _as_text(server.get("revocation_endpoint"))
    binding_problem = _oauth_binding_error(connector_row, metadata=metadata, values=values)
    if not revocation_endpoint or not token or binding_problem:
        return None
    client_id = _as_text(values.get(MCP_OAUTH_CLIENT_ID_SECRET))
    client_secret_value = values.get(MCP_OAUTH_CLIENT_SECRET_SECRET)
    client_secret = client_secret_value if isinstance(client_secret_value, str) else ""
    registration = _as_dict(metadata.get("registration"))
    token_endpoint_auth_method = _as_text(registration.get("token_endpoint_auth_method")) or (
        "client_secret_basic" if client_secret else "none"
    )
    data, basic_auth = _oauth_client_auth(
        {"token": token},
        method=token_endpoint_auth_method,
        client_id=client_id,
        client_secret=client_secret,
    )
    return {
        "endpoint": revocation_endpoint,
        "data": data,
        "basic_auth": basic_auth,
    }


def execute_prepared_mcp_connector_revocation(prepared: dict[str, Any] | None) -> None:
    """Best-effort remote revoke using material captured before local deletion."""
    if not prepared:
        return
    with contextlib.suppress(Exception):
        _post_form(
            _as_text(prepared.get("endpoint")),
            _as_dict(prepared.get("data")),
            purpose="MCP OAuth revocation endpoint",
            basic_auth=prepared.get("basic_auth"),
        )


def schedule_prepared_mcp_connector_revocation(
    conn: Any,
    prepared: dict[str, Any] | None,
) -> None:
    """Run a captured remote revoke only after the surrounding DB commit.

    Revocation is best effort. Without a managed commit boundary the remote
    side effect is skipped, because revoking before an unverified commit could
    leave a locally connected row whose provider grant is already invalid.
    """
    if prepared:
        register_transaction_after_commit(
            conn,
            lambda: execute_prepared_mcp_connector_revocation(prepared),
        )


def prepare_mcp_connector_oauth_delete_locked(
    conn: Any,
    *,
    connector_row: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Capture revoke data and purge local OAuth material in the caller transaction."""
    prepared = _prepare_mcp_connector_grant_revocation(conn, connector_row=connector_row)
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    for ref_key in _refs_map(connector_row).values():
        model_provider_store.delete_secret_ref(
            conn,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            ref_key=ref_key,
        )
    mcp_connector_oauth_store.delete_connector_oauth_states(
        conn,
        connector_id=connector_id,
        account_id=account_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )
    metadata = _oauth_metadata_for_update(conn, connector_row)
    retained_metadata = {
        key: metadata[key]
        for key in (
            "requested_scopes",
            "authorization_parameters",
            "refresh_token_required",
        )
        if key in metadata
    }
    return prepared, retained_metadata


def prepare_mcp_connector_oauth_reset(
    conn: Any,
    *,
    connector_row: dict[str, Any],
) -> dict[str, Any]:
    """Lock OAuth state and defer remote revocation until the local reset commits."""
    locked_row = _locked_connector_row(conn, connector_row)
    _require_current_oauth_revision(connector_row, locked_row)
    prepared = _prepare_mcp_connector_grant_revocation(conn, connector_row=locked_row)
    schedule_prepared_mcp_connector_revocation(conn, prepared)
    return locked_row


def revoke_mcp_connector_oauth(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    clear_connector_state: bool = True,
) -> None:
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, connector_row)
        _require_current_oauth_revision(connector_row, locked_row)
        _revoke_mcp_connector_oauth_locked(
            transaction_conn,
            connector_row=locked_row,
            clear_connector_state=clear_connector_state,
        )


def _revoke_mcp_connector_oauth_locked(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    clear_connector_state: bool,
) -> None:
    metadata = _oauth_metadata(connector_row)
    prepared_revocation = _prepare_mcp_connector_grant_revocation(
        conn,
        connector_row=connector_row,
    )
    account_id = _as_text(connector_row.get("account_id"))
    project_id = _as_text(connector_row.get("project_id"))
    tenant_id = _as_text(connector_row.get("tenant_id"))
    refs = _refs_map(connector_row)
    remaining_refs: dict[str, str] = {}
    for name, ref_key in refs.items():
        if clear_connector_state and name in _MCP_OAUTH_APPLICATION_SECRET_NAMES:
            remaining_refs[name] = ref_key
            continue
        model_provider_store.delete_secret_ref(
            conn,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            ref_key=ref_key,
        )
    if clear_connector_state:
        mcp_connector_oauth_store.delete_connector_oauth_states(
            conn,
            connector_id=_as_text(connector_row.get("connector_id")),
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
        try:
            current_revision = max(1, int(connector_row.get("oauth_credential_revision") or 1))
        except (TypeError, ValueError):
            current_revision = 1
        retained_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {"token", "last_scope_challenge"}
        }
        _update_connector_oauth(
            conn,
            _as_text(connector_row.get("connector_id")),
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            updates={
                "oauth_secret_refs_jsonb": {"refs": remaining_refs} if remaining_refs else {},
                "auth_status": "auth_required",
                "oauth_metadata_jsonb": retained_metadata,
                "oauth_credential_revision": current_revision + 1,
                "oauth_connected_identity": "",
                "oauth_last_error": "",
            },
        )
    schedule_prepared_mcp_connector_revocation(conn, prepared_revocation)
