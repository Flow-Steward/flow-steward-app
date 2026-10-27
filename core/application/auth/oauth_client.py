"""OAuth authorization, callback transactions and connection revocation."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from core.application.auth.oauth_client_provider import (
    _access_token_secret_name,
    _client_credential_value,
    _http_post_form,
    _provider_credential_generation,
    _selected_scopes,
)
from core.application.auth.oauth_client_provider import (
    _assert_https_unless_dev as _assert_https_unless_dev,
)
from core.application.auth.oauth_client_provider import (
    exchange_authorization_code as exchange_authorization_code,
)
from core.application.auth.oauth_client_provider import (
    exchange_refresh_token as exchange_refresh_token,
)
from core.application.auth.oauth_client_provider import (
    normalize_oauth_auth_config as normalize_oauth_auth_config,
)
from core.application.auth.oauth_client_provider import (
    oauth_auth_config_for_connection as oauth_auth_config_for_connection,
)
from core.application.auth.oauth_client_tokens import (
    _granted_scopes as _granted_scopes,
)
from core.application.auth.oauth_client_tokens import (
    _resolve_oauth_secret_values,
)
from core.application.auth.oauth_client_tokens import (
    _token_expires_at as _token_expires_at,
)
from core.application.auth.oauth_client_tokens import (
    ensure_valid_oauth_access_token as ensure_valid_oauth_access_token,
)
from core.application.auth.oauth_client_tokens import (
    oauth_connection_status as oauth_connection_status,
)
from core.application.auth.oauth_client_tokens import (
    resolve_oauth_secret_values as resolve_oauth_secret_values,
)
from core.application.auth.oauth_client_tokens import (
    resolve_oauth_secrets_for_runtime as resolve_oauth_secrets_for_runtime,
)
from core.application.auth.oauth_client_tokens import (
    store_oauth_token_response as store_oauth_token_response,
)
from core.application.auth.oauth_client_values import (
    _MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS,
    _MESSAGE_OAUTH_PROVIDER_CREDENTIALS_CHANGED_RETRY_AUTHORIZATION,
    _MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED,
    _as_dict,
    _as_string_list,
    _as_text,
    _now,
)
from core.application.auth.oauth_client_values import (
    OAUTH_ACCESS_TOKEN_SECRET_DEFAULT as OAUTH_ACCESS_TOKEN_SECRET_DEFAULT,
)
from core.application.auth.oauth_client_values import (
    OAUTH_CALLBACK_PATH as OAUTH_CALLBACK_PATH,
)
from core.application.auth.oauth_client_values import (
    OAUTH_EXPIRES_AT_SECRET as OAUTH_EXPIRES_AT_SECRET,
)
from core.application.auth.oauth_client_values import (
    OAUTH_GRANTED_SCOPES_SECRET as OAUTH_GRANTED_SCOPES_SECRET,
)
from core.application.auth.oauth_client_values import (
    OAUTH_INTERNAL_SECRET_PREFIX as OAUTH_INTERNAL_SECRET_PREFIX,
)
from core.application.auth.oauth_client_values import (
    OAUTH_REFRESH_SKEW_SECONDS as OAUTH_REFRESH_SKEW_SECONDS,
)
from core.application.auth.oauth_client_values import (
    OAUTH_REFRESH_TOKEN_SECRET as OAUTH_REFRESH_TOKEN_SECRET,
)
from core.application.auth.oauth_client_values import (
    OAUTH_STATE_TTL_SECONDS as OAUTH_STATE_TTL_SECONDS,
)
from core.application.auth.oauth_client_values import (
    OAUTH_TOKEN_TYPE_SECRET as OAUTH_TOKEN_TYPE_SECRET,
)
from core.application.auth.oauth_client_values import (
    OAuthConnectionError as OAuthConnectionError,
)
from core.application.auth.oauth_client_values import (
    OAuthTokenEndpointError as OAuthTokenEndpointError,
)
from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.application.extensions import url_safety
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _state_signing_key() -> bytes:
    raw = _as_text(_runtime_env.get("FS_SECRET_KEY"))
    if not raw:
        raise OAuthConnectionError("FS_SECRET_KEY is required for OAuth state signing")
    return raw.encode("utf-8")


def _state_signature(payload_b64: str) -> str:
    digest = hmac.new(_state_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _state_hash(state: str) -> str:
    return hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()


def _build_state_token(payload: dict[str, Any]) -> str:
    encoded_payload = json.dumps(
        dict(payload or {}),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload_b64 = _b64url_encode(encoded_payload)
    return f"{payload_b64}.{_state_signature(payload_b64)}"


def _verify_state_token(state: str, *, now: float | None = None) -> dict[str, Any]:
    state_text = _as_text(state)
    payload_b64, sep, signature = state_text.partition(".")
    if not payload_b64 or not sep or not signature:
        raise OAuthConnectionError("Invalid OAuth state")
    expected = _state_signature(payload_b64)
    if not hmac.compare_digest(signature, expected):
        raise OAuthConnectionError("Invalid OAuth state signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise OAuthConnectionError("Invalid OAuth state payload") from exc
    if not isinstance(payload, dict):
        raise OAuthConnectionError("Invalid OAuth state payload")
    try:
        expires_at = float(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise OAuthConnectionError("Invalid OAuth state expiry") from exc
    if expires_at <= float(now if now is not None else _now()):
        raise OAuthConnectionError("OAuth state has expired")
    return payload


def oauth_state_type(state: str, *, now: float | None = None) -> str:
    payload = _verify_state_token(state, now=now)
    return _as_text(payload.get("type")) or "extension_connection"


def _code_verifier() -> str:
    return _b64url_encode(secrets.token_bytes(48))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(str(verifier).encode("ascii")).digest()
    return _b64url_encode(digest)


def sanitize_oauth_return_path(return_path: str, *, fallback: str) -> str:
    candidate = _as_text(return_path) or fallback
    parsed = urlparse(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or not candidate.startswith("/")
        or candidate.startswith("//")
    ):
        return fallback
    return candidate


def oauth_provider_error_reason(provider_error: str) -> str:
    """Map provider callback errors to the closed, user-facing reason vocabulary."""
    return (
        "authorization_cancelled"
        if _as_text(provider_error).lower() == "access_denied"
        else "oauth_failed"
    )


def oauth_callback_url() -> str:
    from core.application.auth.serve_auth_runtime import public_base_url

    base_url = _as_text(public_base_url()).rstrip("/")
    if not base_url:
        raise OAuthConnectionError("FS_PUBLIC_BASE_URL is required for OAuth callbacks")
    parsed = urlparse(base_url)
    loopback = str(parsed.hostname or "").lower() == "localhost"
    if not loopback:
        with contextlib.suppress(ValueError):
            import ipaddress

            loopback = ipaddress.ip_address(str(parsed.hostname or "")).is_loopback
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and loopback and url_safety._allow_private_remote_urls()
    ):
        raise OAuthConnectionError(
            "OAuth callback origin must use https except explicit loopback development"
        )
    return f"{base_url}{OAUTH_CALLBACK_PATH}"


def start_oauth_authorization(
    conn: Any,
    *,
    connection: dict[str, Any],
    auth_config: dict[str, Any],
    redirect_uri: str,
    optional_scopes: list[str] | None = None,
    return_path: str = "",
    now: float | None = None,
    transaction_conn: Any | None = None,
) -> dict[str, Any]:
    from core.infrastructure.db import project_connection_oauth_store

    normalized_auth = normalize_oauth_auth_config(auth_config)
    started_at = float(now if now is not None else _now())
    expires_at = started_at + OAUTH_STATE_TTL_SECONDS
    connection_id = _as_text(connection.get("connection_id"))
    account_id = _as_text(connection.get("account_id"))
    project_id = _as_text(connection.get("project_id"))
    if not connection_id or not account_id or not project_id:
        raise OAuthConnectionError("OAuth connection identity is incomplete")

    provider_generation = _provider_credential_generation(
        conn,
        normalized_auth,
        connection=connection,
    )
    client_id = _client_credential_value(
        credential_store=conn,
        auth_config=normalized_auth,
        connection=connection,
        credential_name="client_id",
        required=True,
    )
    client_secret = _client_credential_value(
        credential_store=conn,
        auth_config=normalized_auth,
        connection=connection,
        credential_name="client_secret",
        required=not bool(
            _as_dict(normalized_auth.get("client_credentials"))
            .get("client_secret", {})
            .get("optional", False)
        ),
    )
    _ = client_secret
    if (
        _provider_credential_generation(conn, normalized_auth, connection=connection)
        != provider_generation
    ):
        raise OAuthConnectionError(_MESSAGE_OAUTH_PROVIDER_CREDENTIALS_CHANGED_RETRY_AUTHORIZATION)

    verifier = _code_verifier()
    scopes = _selected_scopes(normalized_auth, optional_scopes)
    fallback_path = f"/projects/{quote(project_id)}/connections/{quote(connection_id)}"
    safe_return_path = sanitize_oauth_return_path(return_path, fallback=fallback_path)
    state_payload = {
        "v": 1,
        "type": "extension_connection",
        "nonce": secrets.token_urlsafe(18),
        "connection_id": connection_id,
        "account_id": account_id,
        "project_id": project_id,
        "client_id": client_id,
        "exp": int(expires_at),
    }
    state = _build_state_token(state_payload)
    auth_payload = {
        "auth_config": normalized_auth,
        "client_id": client_id,
        "scopes": scopes,
        "provider_credential_generation": provider_generation,
    }
    from core.infrastructure.db.db_pool import transaction_connection

    if transaction_conn is not None:
        transaction = contextlib.nullcontext(transaction_conn)
    elif callable(getattr(getattr(conn, "conn", conn), "cursor", None)):
        transaction = transaction_connection(conn)
    else:
        transaction = contextlib.nullcontext(conn)
    with transaction as transaction_conn:
        project_connection_oauth_store.replace_connection_oauth_state(
            transaction_conn,
            state_hash=_state_hash(state),
            connection_id=connection_id,
            account_id=account_id,
            project_id=project_id,
            code_verifier_encrypted=encrypt_secret(verifier),
            redirect_uri=redirect_uri,
            return_path=safe_return_path,
            auth_payload=auth_payload,
            expires_at=expires_at,
        )

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    for key, value in _as_dict(normalized_auth.get("authorization_params")).items():
        if key not in auth_params and value is not None:
            auth_params[str(key)] = str(value)
    authorization_url = f"{normalized_auth['authorization_endpoint']}?{urlencode(auth_params)}"
    return {
        "authorization_url": authorization_url,
        "connection_id": connection_id,
        "project_id": project_id,
        "expires_at": expires_at,
        "scopes": scopes,
        "redirect_uri": redirect_uri,
    }


def oauth_callback_transaction(conn: Any):
    """Open the existing callback transaction boundary for a prepared exchange."""
    from core.infrastructure.db.db_pool import transaction_connection

    return (
        transaction_connection(conn)
        if callable(getattr(getattr(conn, "conn", conn), "cursor", None))
        else contextlib.nullcontext(conn)
    )


def prepare_oauth_callback(
    conn: Any,
    *,
    state: str,
    code: str,
    now: float | None = None,
    token_exchange: Callable[..., dict[str, Any]] = exchange_authorization_code,
) -> dict[str, Any]:
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store

    code_text = _as_text(code)
    if not code_text:
        raise OAuthConnectionError("OAuth callback code is required")
    state_payload = _verify_extension_connection_state(state, now=now)
    with oauth_callback_transaction(conn) as transaction_conn:
        connection = project_connection_store.get_project_connection_row_for_update(
            transaction_conn,
            _as_text(state_payload.get("connection_id")),
        )
        if not connection:
            raise OAuthConnectionError(_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS)
        _validate_stored_oauth_state(connection, state_payload)
        state_hash = _state_hash(state)
        row = project_connection_oauth_store.consume_oauth_state(
            transaction_conn, state_hash=state_hash
        )
        if not row:
            raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
        _validate_stored_oauth_state(row, state_payload)

    auth_payload = _as_dict(row.get("auth_payload_jsonb"))
    auth_config = normalize_oauth_auth_config(_as_dict(auth_payload.get("auth_config")))
    stored_generation = int(auth_payload.get("provider_credential_generation") or 0)
    if (
        _provider_credential_generation(conn, auth_config, connection=connection)
        != stored_generation
    ):
        raise OAuthConnectionError(_MESSAGE_OAUTH_PROVIDER_CREDENTIALS_CHANGED_RETRY_AUTHORIZATION)
    client_id = _as_text(auth_payload.get("client_id")) or _client_credential_value(
        credential_store=conn,
        auth_config=auth_config,
        connection=connection,
        credential_name="client_id",
        required=True,
    )
    client_secret = _client_credential_value(
        credential_store=conn,
        auth_config=auth_config,
        connection=connection,
        credential_name="client_secret",
        required=not bool(
            _as_dict(auth_config.get("client_credentials"))
            .get("client_secret", {})
            .get("optional", False)
        ),
    )
    verifier = decrypt_secret(_as_text(row.get("code_verifier_encrypted")))
    connection_stamp = {
        "credential_revision": connection.get("credential_revision"),
        "updated_at": connection.get("updated_at"),
    }
    callback_context = {
        "connection_id": _as_text(connection.get("connection_id")),
        "account_id": _as_text(connection.get("account_id")),
        "project_id": _as_text(connection.get("project_id")),
        "client_id": client_id,
        "provider_credential_generation": stored_generation,
        "return_path": sanitize_oauth_return_path(
            _as_text(row.get("return_path")),
            fallback=f"/projects/{quote(_as_text(connection.get('project_id')))}/connections/"
            f"{quote(_as_text(connection.get('connection_id')))}",
        ),
    }

    try:
        token_response = token_exchange(
            auth_config=auth_config,
            code=code_text,
            redirect_uri=_as_text(row.get("redirect_uri")),
            code_verifier=verifier,
            client_id=client_id,
            client_secret=client_secret,
        )
    except OAuthTokenEndpointError as exc:
        exc.callback_context = callback_context
        raise

    return {
        "connection": connection,
        "state_payload": state_payload,
        "state_row": row,
        "auth_payload": auth_payload,
        "auth_config": auth_config,
        "stored_generation": stored_generation,
        "token_response": token_response,
        "connection_stamp": connection_stamp,
        "callback_context": callback_context,
        "now": now,
    }


def validate_prepared_oauth_callback_for_commit(
    conn: Any,
    transaction_conn: Any,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    """Revalidate a prepared callback under the connection-row lock."""
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store

    state_payload = _as_dict(prepared.get("state_payload"))
    latest_connection = project_connection_store.get_project_connection_row_for_update(
        transaction_conn,
        _as_text(state_payload.get("connection_id")),
    )
    if not latest_connection:
        raise OAuthConnectionError(_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS)
    _validate_stored_oauth_state(latest_connection, state_payload)
    connection_stamp = _as_dict(prepared.get("connection_stamp"))
    if (
        connection_stamp.get("credential_revision") is not None
        and latest_connection.get("credential_revision")
        != connection_stamp.get("credential_revision")
    ) or (
        connection_stamp.get("updated_at") is not None
        and latest_connection.get("updated_at") != connection_stamp.get("updated_at")
    ):
        raise OAuthConnectionError("OAuth connection changed; restart authorization")
    if callable(getattr(transaction_conn, "cursor", None)) and (
        project_connection_oauth_store.has_live_connection_oauth_state(
            transaction_conn,
            connection_id=_as_text(latest_connection.get("connection_id")),
        )
    ):
        raise OAuthConnectionError("OAuth authorization was superseded by a newer attempt")
    auth_config = _as_dict(prepared.get("auth_config"))
    if _provider_credential_generation(
        conn,
        auth_config,
        connection=latest_connection,
    ) != int(prepared.get("stored_generation") or 0):
        raise OAuthConnectionError(_MESSAGE_OAUTH_PROVIDER_CREDENTIALS_CHANGED_RETRY_AUTHORIZATION)
    return latest_connection


def commit_oauth_callback(
    conn: Any,
    *,
    prepared: dict[str, Any],
    oauth_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit one prepared token response after final connection revalidation."""
    with oauth_callback_transaction(conn) as transaction_conn:
        latest_connection = validate_prepared_oauth_callback_for_commit(
            conn,
            transaction_conn,
            prepared,
        )
        updated_connection = store_oauth_token_response(
            transaction_conn,
            connection=latest_connection,
            auth_config=_as_dict(prepared.get("auth_config")),
            token_response=_as_dict(prepared.get("token_response")),
            granted_scope_fallback=_as_string_list(
                _as_dict(prepared.get("auth_payload")).get("scopes")
            ),
            oauth_metadata=oauth_metadata,
            now=prepared.get("now"),
        )
    callback_context = _as_dict(prepared.get("callback_context"))
    return {
        "status": "connected",
        "connection_id": _as_text(updated_connection.get("connection_id")),
        "project_id": _as_text(updated_connection.get("project_id")),
        "return_path": _as_text(callback_context.get("return_path")),
    }


def handle_oauth_callback(
    conn: Any,
    *,
    state: str,
    code: str,
    now: float | None = None,
    token_exchange: Callable[..., dict[str, Any]] = exchange_authorization_code,
) -> dict[str, Any]:
    prepared = prepare_oauth_callback(
        conn,
        state=state,
        code=code,
        now=now,
        token_exchange=token_exchange,
    )
    return commit_oauth_callback(conn, prepared=prepared)


def _verify_extension_connection_state(state: str, *, now: float | None = None) -> dict[str, Any]:
    payload = _verify_state_token(state, now=now)
    if _as_text(payload.get("type") or "extension_connection") != "extension_connection":
        raise OAuthConnectionError("OAuth state is not for a project connection")
    return payload


def verify_extension_connection_state(
    state: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate a signed project-connection OAuth state token."""
    return _verify_extension_connection_state(state, now=now)


def oauth_state_hash(state: str) -> str:
    """Return the stable database lookup hash for an OAuth state token."""
    return _state_hash(state)


def oauth_callback_context(
    conn: Any,
    *,
    state: str,
    fallback_return_path: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Read validated callback routing context without consuming one-time state."""
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store

    state_payload = verify_extension_connection_state(state, now=now)
    state_row = project_connection_oauth_store.get_oauth_state(
        getattr(conn, "conn", conn),
        state_hash=oauth_state_hash(state),
    )
    if not state_row:
        raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
    _validate_stored_oauth_state(state_row, state_payload)
    connection = project_connection_store.get_project_connection_row(
        conn,
        _as_text(state_row.get("connection_id")),
    )
    if not connection:
        raise OAuthConnectionError(_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS)
    return {
        "connection_id": _as_text(connection.get("connection_id")),
        "project_id": _as_text(connection.get("project_id")),
        "extension_id": _as_text(connection.get("extension_id")),
        "connection_type": _as_text(connection.get("connection_type")),
        "return_path": sanitize_oauth_return_path(
            _as_text(state_row.get("return_path")),
            fallback=fallback_return_path,
        ),
    }


def _validate_stored_oauth_state(stored: dict[str, Any], state_payload: dict[str, Any]) -> None:
    for key in ("connection_id", "account_id", "project_id"):
        if _as_text(stored.get(key)) != _as_text(state_payload.get(key)):
            raise OAuthConnectionError("OAuth state does not match the stored connection")


def handle_oauth_provider_error(
    conn: Any,
    *,
    state: str,
    provider_error: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Consume a valid one-time project OAuth state without exchanging a code."""
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store
    from core.infrastructure.db.db_pool import transaction_connection

    state_payload = _verify_extension_connection_state(state, now=now)
    transaction = (
        transaction_connection(conn)
        if callable(getattr(getattr(conn, "conn", conn), "cursor", None))
        else contextlib.nullcontext(conn)
    )
    with transaction as transaction_conn:
        connection = project_connection_store.get_project_connection_row_for_update(
            transaction_conn,
            _as_text(state_payload.get("connection_id")),
        )
        if not connection:
            raise OAuthConnectionError(_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS)
        _validate_stored_oauth_state(connection, state_payload)
        row = project_connection_oauth_store.consume_oauth_state(
            transaction_conn,
            state_hash=_state_hash(state),
        )
        if not row:
            raise OAuthConnectionError(_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED)
        _validate_stored_oauth_state(row, state_payload)
    return {
        "status": "pending",
        "connection_id": _as_text(row.get("connection_id")),
        "project_id": _as_text(row.get("project_id")),
        "return_path": _as_text(row.get("return_path")),
        "provider_error": _as_text(provider_error) or "access_denied",
    }


def revoke_connection_oauth_tokens(
    conn: Any,
    *,
    connection: dict[str, Any],
    delete_local: bool = True,
) -> None:
    try:
        revocation = prepare_connection_oauth_revocation(conn, connection=connection)
        if revocation:
            execute_prepared_oauth_revocation(revocation)
    except Exception:
        pass

    if not delete_local:
        return
    auth_config = oauth_auth_config_for_connection(conn, connection)
    if not auth_config:
        return
    from core.application.management.project_connection_secrets import secret_ref_names
    from core.infrastructure.db import model_provider_store

    access_name = _access_token_secret_name(auth_config)
    account_id = _as_text(connection.get("account_id"))
    refs = _as_dict(connection.get("secret_refs")).get("refs")
    if not isinstance(refs, dict):
        return
    allowed_names = set(secret_ref_names(connection.get("secret_refs")))
    for secret_name, ref_key in refs.items():
        normalized_name = _as_text(secret_name)
        if normalized_name not in allowed_names:
            continue
        if normalized_name != access_name and not normalized_name.startswith(
            OAUTH_INTERNAL_SECRET_PREFIX
        ):
            continue
        with contextlib.suppress(Exception):
            model_provider_store.delete_secret_ref(
                conn,
                account_id=account_id,
                ref_key=_as_text(ref_key),
            )


def prepare_connection_oauth_revocation(
    conn: Any,
    *,
    connection: dict[str, Any],
) -> dict[str, Any] | None:
    """Capture one revocation request without performing external I/O."""
    auth_config = oauth_auth_config_for_connection(conn, connection)
    if not auth_config:
        return None
    values = _resolve_oauth_secret_values(conn, connection)
    refresh_token = _as_text(values.get(OAUTH_REFRESH_TOKEN_SECRET))
    access_name = _access_token_secret_name(auth_config)
    token_for_revoke = refresh_token or _as_text(values.get(access_name))
    revocation_endpoint = _as_text(auth_config.get("revocation_endpoint"))
    if not revocation_endpoint or not token_for_revoke:
        return None
    client_id = _client_credential_value(
        credential_store=conn,
        auth_config=auth_config,
        connection=connection,
        credential_name="client_id",
        required=True,
    )
    client_secret = _client_credential_value(
        credential_store=conn,
        auth_config=auth_config,
        connection=connection,
        credential_name="client_secret",
        required=False,
    )
    data = {"token": token_for_revoke, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    return {"endpoint": revocation_endpoint, "data": data}


def execute_prepared_oauth_revocation(revocation: dict[str, Any]) -> bool:
    """Best-effort external revoke for credentials captured before local cleanup."""
    endpoint = _as_text(revocation.get("endpoint"))
    raw_data = revocation.get("data")
    data = (
        {
            _as_text(key): _as_text(value)
            for key, value in raw_data.items()
            if _as_text(key) and _as_text(value)
        }
        if isinstance(raw_data, dict)
        else {}
    )
    if not endpoint or not data.get("token") or not data.get("client_id"):
        return False
    try:
        _http_post_form(endpoint, data, purpose="OAuth revocation endpoint")
    except Exception:
        return False
    return True


def disconnect_connection_oauth(conn: Any, *, connection: dict[str, Any]) -> dict[str, Any]:
    """Clear a legacy connection-local grant atomically while preserving safe identity."""
    from core.application.management import (
        project_connection_secrets,
        project_connection_sessions,
    )
    from core.infrastructure.db import project_connection_oauth_store, project_connection_store
    from core.infrastructure.db.db_pool import transaction_connection

    revoke_connection_oauth_tokens(conn, connection=connection, delete_local=False)
    connection_id = _as_text(connection.get("connection_id"))
    account_id = _as_text(connection.get("account_id"))
    with transaction_connection(conn) as transaction_conn:
        locked_connection = project_connection_store.get_project_connection_row_for_update(
            transaction_conn,
            connection_id,
        )
        if not locked_connection or _as_text(locked_connection.get("account_id")) != account_id:
            raise OAuthConnectionError(_MESSAGE_OAUTH_CONNECTION_NO_LONGER_EXISTS)
        config = _as_dict(locked_connection.get("connection_config"))
        previous_oauth = _as_dict(config.get("oauth"))
        oauth: dict[str, Any] = {
            "lifecycle_status": "disconnected",
            **{
                key: previous_oauth[key]
                for key in (
                    "subject",
                    "verified_display_name",
                    "verified_email",
                    "safe_metadata",
                )
                if key in previous_oauth
            },
        }
        config["oauth"] = oauth
        project_connection_oauth_store.invalidate_connection_oauth_states(
            transaction_conn,
            connection_id=connection_id,
        )
        project_connection_secrets.delete_connection_secret_refs(
            transaction_conn,
            account_id=account_id,
            connection_id=connection_id,
        )
        project_connection_sessions.invalidate_connection_sessions(
            transaction_conn,
            connection_id=connection_id,
        )
        project_connection_store.update_project_connection_row(
            transaction_conn,
            connection_id,
            {
                "secret_refs": {},
                "connection_config": config,
                "health_status": "unknown",
                "runtime_status": "unavailable",
                "last_error": "",
            },
        )
    return {
        "status": "disconnected",
        "connection_id": connection_id,
        "verified_email": _as_text(previous_oauth.get("verified_email")) or None,
    }
