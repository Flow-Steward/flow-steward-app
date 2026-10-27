"""Discover MCP OAuth providers and register clients using pinned HTTP requests."""

from __future__ import annotations

import base64
import contextlib
import json
import urllib.error
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urlparse
from urllib.request import Request

from core.application.auth import oauth_client
from core.application.auth.mcp_connector_oauth_policy import (
    _MCP_OAUTH_APPLICATION_SECRET_NAMES,
    _OAUTH_ERROR_CODE_PATTERN,
    MCP_OAUTH_CLIENT_ID_SECRET,
    MCP_OAUTH_CLIENT_SECRET_SECRET,
    MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    McpOAuthManualCredentialsRequired,
    _as_dict,
    _as_string_list,
    _as_text,
    _bearer_challenge,
    _connector_lineage,
    _current_connector_endpoint,
    _is_https_url,
    _manual_token_endpoint_auth_method,
    _merge_refs,
    _now,
    _oauth_credential_revision,
    _oauth_metadata,
    _protected_resource_metadata_candidates,
    _refs_map,
    _registration_issuer_binding_error,
    _require_current_oauth_revision,
    _validate_authorization_server_metadata,
    _validate_metadata_url,
    _validated_mcp_resource,
    authorization_server_metadata_urls,
)
from core.application.auth.mcp_connector_oauth_repository import (
    _locked_connector_row,
    _mark_auth_problem_if_current,
    _oauth_metadata_for_update,
    _resolve_connector_oauth_secrets,
    _save_connector_oauth_secrets,
    _update_connector_oauth,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.extensions import url_safety
from core.application.management.errors import (
    ServiceValidationError,
)
from core.application.management.mcp_connector_http import (
    read_bounded_http_body,
)
from core.application.management.mcp_connector_store import McpConnectorStore
from core.infrastructure.db import model_provider_store
from core.infrastructure.db.db_pool import (
    register_transaction_after_rollback,
    transaction_connection,
)

_TOKEN_APPLICATION_JSON = "application/json"


_MAX_OAUTH_HTTP_RESPONSE_BYTES = 1024 * 1024


def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0) -> dict[str, Any]:
    safe_url = _validate_metadata_url(url, purpose=purpose)
    request = Request(  # nosec B310
        safe_url,
        headers={"Accept": _TOKEN_APPLICATION_JSON},
        method="GET",
    )
    try:
        with url_safety.open_pinned_url(
            request,
            timeout_seconds=max(1.0, float(timeout_seconds)),
            purpose=purpose,
            require_private=urlparse(safe_url).scheme.lower() == "http",
        ) as response:  # nosec B310
            raw = read_bounded_http_body(
                response,
                max_bytes=_MAX_OAUTH_HTTP_RESPONSE_BYTES,
                purpose=purpose,
            ).decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OAuthConnectionError(f"{purpose} returned HTTP {exc.code}") from exc
    except ServiceValidationError as exc:
        raise OAuthConnectionError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, OAuthConnectionError):
            raise
        raise OAuthConnectionError(f"{purpose} request failed: {exc}") from exc
    try:
        parsed = json.loads(raw or "{}")
    except Exception as exc:
        raise OAuthConnectionError(f"{purpose} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise OAuthConnectionError(f"{purpose} returned invalid JSON")
    return parsed


def _oauth_http_error_message(purpose: str, exc: urllib.error.HTTPError) -> str:
    error_code = ""
    body = ""
    with contextlib.suppress(Exception):
        body = read_bounded_http_body(
            exc,
            max_bytes=_MAX_OAUTH_HTTP_RESPONSE_BYTES,
            purpose=f"{purpose} error response",
        ).decode("utf-8", errors="replace")
    with contextlib.suppress(Exception):
        parsed = json.loads(body or "{}")
        if isinstance(parsed, dict):
            candidate = _as_text(parsed.get("error"))
            if _OAUTH_ERROR_CODE_PATTERN.fullmatch(candidate):
                error_code = candidate
    suffix = f" ({error_code})" if error_code else ""
    return f"{purpose} returned HTTP {exc.code}{suffix}"


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    purpose: str,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    safe_url = _validate_metadata_url(url, purpose=purpose)
    request = Request(  # nosec B310
        safe_url,
        data=json.dumps(dict(payload or {}), separators=(",", ":")).encode("utf-8"),
        headers={"Accept": _TOKEN_APPLICATION_JSON, "Content-Type": _TOKEN_APPLICATION_JSON},
        method="POST",
    )
    try:
        with url_safety.open_pinned_url(
            request,
            timeout_seconds=max(1.0, float(timeout_seconds)),
            purpose=purpose,
            require_private=urlparse(safe_url).scheme.lower() == "http",
        ) as response:  # nosec B310
            raw = read_bounded_http_body(
                response,
                max_bytes=_MAX_OAUTH_HTTP_RESPONSE_BYTES,
                purpose=purpose,
            ).decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OAuthConnectionError(_oauth_http_error_message(purpose, exc)) from exc
    except ServiceValidationError as exc:
        raise OAuthConnectionError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, OAuthConnectionError):
            raise
        raise OAuthConnectionError(f"{purpose} request failed: {exc}") from exc
    try:
        parsed = json.loads(raw or "{}")
    except Exception as exc:
        raise OAuthConnectionError(f"{purpose} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise OAuthConnectionError(f"{purpose} returned invalid JSON")
    return parsed


def _post_form(
    url: str,
    data: dict[str, str],
    *,
    purpose: str,
    basic_auth: tuple[str, str] | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    safe_url = _validate_metadata_url(url, purpose=purpose)
    headers = {
        "Accept": _TOKEN_APPLICATION_JSON,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if basic_auth is not None:
        user, password = basic_auth
        encoded = base64.b64encode(
            f"{quote_plus(user, safe='')}:{quote_plus(password, safe='')}".encode()
        ).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    request = Request(  # nosec B310
        safe_url,
        data=urlencode(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with url_safety.open_pinned_url(
            request,
            timeout_seconds=max(1.0, float(timeout_seconds)),
            purpose=purpose,
            require_private=urlparse(safe_url).scheme.lower() == "http",
        ) as response:  # nosec B310
            raw = read_bounded_http_body(
                response,
                max_bytes=_MAX_OAUTH_HTTP_RESPONSE_BYTES,
                purpose=purpose,
            ).decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OAuthConnectionError(_oauth_http_error_message(purpose, exc)) from exc
    except ServiceValidationError as exc:
        raise OAuthConnectionError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, OAuthConnectionError):
            raise
        raise OAuthConnectionError(f"{purpose} request failed: {exc}") from exc
    try:
        parsed = json.loads(raw or "{}")
    except Exception as exc:
        raise OAuthConnectionError(f"{purpose} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise OAuthConnectionError(f"{purpose} returned invalid JSON")
    return parsed


def _delete_dynamic_client_registration(
    registration_client_uri: str,
    registration_access_token: str,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    """Best-effort RFC 7592 cleanup for a DCR client not persisted locally."""
    purpose = "MCP OAuth dynamic client registration cleanup endpoint"
    safe_url = _validate_metadata_url(registration_client_uri, purpose=purpose)
    token = _as_text(registration_access_token)
    if not token:
        return
    request = Request(  # nosec B310
        safe_url,
        headers={"Accept": _TOKEN_APPLICATION_JSON, "Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    with url_safety.open_pinned_url(
        request,
        timeout_seconds=max(1.0, float(timeout_seconds)),
        purpose=purpose,
        require_private=urlparse(safe_url).scheme.lower() == "http",
    ) as response:  # nosec B310
        read_bounded_http_body(
            response,
            max_bytes=_MAX_OAUTH_HTTP_RESPONSE_BYTES,
            purpose=purpose,
        )


@contextlib.contextmanager
def _compensate_dynamic_registration_on_error(
    cleanup: Callable[[], None] | None,
):
    try:
        yield
    except BaseException:
        if cleanup is not None:
            with contextlib.suppress(Exception):
                cleanup()
        raise


def discover_mcp_oauth(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    www_authenticate: str | list[str] | tuple[str, ...] | None = None,
    fetch_json: Any = _fetch_json,
    expected_credential_revision: int | None = None,
) -> dict[str, Any]:
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    expected_revision = max(
        1,
        int(expected_credential_revision or _oauth_credential_revision(connector_row)),
    )
    base_url = _as_text(connector_row.get("base_url"))
    challenge = _bearer_challenge(www_authenticate)

    challenge_resource_metadata = _as_text(challenge.get("resource_metadata"))
    prm_candidates = _protected_resource_metadata_candidates(
        base_url,
        challenge_resource_metadata=challenge_resource_metadata,
    )

    protected_resource: dict[str, Any] | None = None
    protected_resource_url = ""
    protected_resource_value = ""
    last_error = ""
    for candidate in prm_candidates:
        candidate_url = _as_text(candidate.get("url"))
        try:
            payload = fetch_json(
                candidate_url,
                purpose="MCP OAuth protected resource metadata",
            )
            authorization_servers = _as_string_list(payload.get("authorization_servers"))
            if not authorization_servers:
                raise OAuthConnectionError(
                    "MCP protected resource metadata must include authorization_servers"
                )
            resource = _validated_mcp_resource(
                payload.get("resource"),
                expected_resource=_as_text(candidate.get("expected_resource")),
            )
            protected_resource = dict(payload)
            protected_resource_value = resource
            protected_resource_url = candidate_url
            break
        except Exception as exc:
            last_error = str(exc)
            continue
    if protected_resource is None:
        _mark_auth_problem_if_current(
            conn,
            connector_row,
            expected_credential_revision=expected_revision,
            auth_status="discovery_required",
            message=last_error or "MCP OAuth protected resource metadata discovery failed",
        )
        raise OAuthConnectionError(
            last_error or "MCP OAuth protected resource metadata discovery failed"
        )

    selected_as: dict[str, Any] | None = None
    selected_issuer = ""
    selected_metadata_url = ""
    for issuer in _as_string_list(protected_resource.get("authorization_servers")):
        for metadata_url in authorization_server_metadata_urls(issuer):
            try:
                payload = fetch_json(
                    metadata_url,
                    purpose="MCP OAuth authorization server metadata",
                )
                selected_as = _validate_authorization_server_metadata(
                    payload,
                    expected_issuer=issuer,
                )
                selected_issuer = _as_text(selected_as.get("issuer"))
                selected_metadata_url = metadata_url
                break
            except Exception as exc:
                last_error = str(exc)
                continue
        if selected_as is not None:
            break
    if selected_as is None:
        _mark_auth_problem_if_current(
            conn,
            connector_row,
            expected_credential_revision=expected_revision,
            auth_status="discovery_required",
            message=last_error or "MCP OAuth authorization server discovery failed",
        )
        raise OAuthConnectionError(last_error or "MCP OAuth authorization server discovery failed")

    scope_from_challenge = _as_string_list(
        _as_text(challenge.get("scope")).replace(",", " ").split() if challenge.get("scope") else []
    )
    discovery = {
        "resource_metadata_url": protected_resource_url,
        "connector_endpoint": _current_connector_endpoint(connector_row),
        "resource": protected_resource_value,
        "authorization_servers": _as_string_list(protected_resource.get("authorization_servers")),
        "scopes_supported": _as_string_list(protected_resource.get("scopes_supported")),
        "challenge_scope": scope_from_challenge,
        "issuer": selected_issuer,
        "authorization_server_metadata_url": selected_metadata_url,
        "authorization_server": selected_as,
        "discovered_at": _now(),
    }
    expected_row = {**connector_row, "oauth_credential_revision": expected_revision}
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, expected_row)
        _require_current_oauth_revision(expected_row, locked_row)
        metadata = _oauth_metadata_for_update(transaction_conn, locked_row)
        metadata["discovery"] = discovery
        _update_connector_oauth(
            transaction_conn,
            connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            updates={
                "oauth_metadata_jsonb": metadata,
                "auth_status": "auth_required",
                "oauth_last_error": "",
            },
        )
    connector_row["oauth_metadata_jsonb"] = metadata
    connector_row["auth_status"] = "auth_required"
    connector_row["oauth_last_error"] = ""
    return discovery


def _oauth_callback_url() -> str:
    return oauth_client.oauth_callback_url()


def mcp_client_metadata_document_url(connector_id: str) -> str:
    from core.application.auth.serve_auth_runtime import public_base_url

    base = _as_text(public_base_url()).rstrip("/")
    if not base:
        raise OAuthConnectionError("FS_PUBLIC_BASE_URL is required for OAuth client metadata")
    return f"{base}/api/mcp/oauth/client-metadata/{quote(_as_text(connector_id))}.json"


def mcp_client_metadata_document(
    connector_row: dict[str, Any] | None = None,
    *,
    connector_id: str = "",
) -> dict[str, Any]:
    resolved_connector_id = _as_text(connector_id) or _as_text(
        (connector_row or {}).get("connector_id")
    )
    if not resolved_connector_id:
        raise OAuthConnectionError("MCP connector_id is required for client metadata")
    client_id = mcp_client_metadata_document_url(resolved_connector_id)
    if not _is_https_url(client_id) and not url_safety._allow_private_remote_urls():
        raise OAuthConnectionError("MCP OAuth Client ID Metadata Document must use https")
    return {
        "client_id": client_id,
        "client_name": "Flow Steward MCP Client",
        "redirect_uris": [_oauth_callback_url()],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _selected_discovery(conn: Any, connector_row: dict[str, Any]) -> dict[str, Any]:
    metadata = _oauth_metadata_for_update(conn, connector_row)
    discovery = _as_dict(metadata.get("discovery"))
    server = _as_dict(discovery.get("authorization_server"))
    if not server:
        connector_row = (
            McpConnectorStore().get_connector_row(
                conn,
                _as_text(connector_row.get("connector_id")),
                account_id=_as_text(connector_row.get("account_id")),
                project_id=_as_text(connector_row.get("project_id")),
                tenant_id=_as_text(connector_row.get("tenant_id")),
            )
            or connector_row
        )
        metadata = _oauth_metadata(connector_row)
        discovery = _as_dict(metadata.get("discovery"))
        server = _as_dict(discovery.get("authorization_server"))
    if not server:
        return discover_mcp_oauth(conn, connector_row=connector_row)
    _validate_authorization_server_metadata(
        server,
        expected_issuer=_as_text(discovery.get("issuer")),
    )
    return discovery


def _client_registration_payload(
    connector_row: dict[str, Any],
    *,
    scopes: list[str],
    token_endpoint_auth_method: str = "none",
) -> dict[str, Any]:
    callback_host = urlparse(_oauth_callback_url()).hostname or ""
    application_type = (
        "native"
        if callback_host in {"localhost", "127.0.0.1", "::1"}
        or callback_host.endswith(".localhost")
        else "web"
    )
    return {
        "client_name": _as_text(connector_row.get("display_name")) or "Flow Steward MCP Client",
        "redirect_uris": [_oauth_callback_url()],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "application_type": application_type,
        "scope": " ".join(scopes),
    }


def _register_client(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    discovery: dict[str, Any],
    scopes: list[str],
    post_json: Any = _post_json,
    expected_credential_revision: int | None = None,
) -> dict[str, Any]:
    expected_revision = max(
        1,
        int(expected_credential_revision or _oauth_credential_revision(connector_row)),
    )
    expected_row = {**connector_row, "oauth_credential_revision": expected_revision}
    with transaction_connection(conn) as transaction_conn:
        locked_row = _locked_connector_row(transaction_conn, expected_row)
        _require_current_oauth_revision(expected_row, locked_row)
        return _register_client_locked(
            transaction_conn,
            connector_row=locked_row,
            discovery=discovery,
            scopes=scopes,
            post_json=post_json,
            expected_credential_revision=expected_revision,
        )


def _register_client_locked(
    conn: Any,
    *,
    connector_row: dict[str, Any],
    discovery: dict[str, Any],
    scopes: list[str],
    post_json: Any = _post_json,
    expected_credential_revision: int | None = None,
) -> dict[str, Any]:
    connector_id, account_id, project_id, tenant_id = _connector_lineage(connector_row)
    expected_revision = max(
        1,
        int(expected_credential_revision or _oauth_credential_revision(connector_row)),
    )
    server = _as_dict(discovery.get("authorization_server"))
    values = _resolve_connector_oauth_secrets(conn, connector_row)
    stored_client_id = _as_text(values.get(MCP_OAUTH_CLIENT_ID_SECRET))
    stored_client_secret_value = values.get(MCP_OAUTH_CLIENT_SECRET_SECRET)
    stored_client_secret = (
        stored_client_secret_value if isinstance(stored_client_secret_value, str) else ""
    )
    metadata = _oauth_metadata_for_update(conn, connector_row)
    registration = _as_dict(metadata.get("registration"))
    force_dynamic_registration = False
    registration_ready = False
    secrets_to_save: dict[str, Any] = {}
    registration_cleanup: Callable[[], None] | None = None

    if stored_client_id and registration:
        registration_mode = _as_text(registration.get("mode")) or "manual"
        registration_issuer = _as_text(registration.get("issuer"))
        discovery_issuer = _as_text(discovery.get("issuer"))
        if registration_mode == "manual":
            discovered_method = _manual_token_endpoint_auth_method(
                server,
                has_client_secret=bool(stored_client_secret),
            )
            registration = {
                **registration,
                "issuer": registration_issuer or discovery_issuer,
                "token_endpoint_auth_method": discovered_method,
            }
        registration_problem = _registration_issuer_binding_error(
            registration,
            discovery=discovery,
        )
        if not registration_problem:
            registration_ready = True
        else:
            if _as_text(registration.get("mode")) != "dynamic_client_registration" or not _as_text(
                server.get("registration_endpoint")
            ):
                _mark_auth_problem_if_current(
                    conn,
                    connector_row,
                    expected_credential_revision=expected_revision,
                    auth_status="auth_required",
                    message=registration_problem,
                )
                raise OAuthConnectionError(registration_problem)
            stored_client_id = ""
            stored_client_secret = ""
            registration = {}
            force_dynamic_registration = True
    if (
        not registration_ready
        and bool(server.get("client_id_metadata_document_supported"))
        and not force_dynamic_registration
    ):
        stored_client_id = mcp_client_metadata_document_url(connector_id)
        stored_client_secret = ""
        registration = {
            "mode": "client_id_metadata_document",
            "issuer": _as_text(discovery.get("issuer")),
            "token_endpoint_auth_method": "none",
        }
    elif not registration_ready and _as_text(server.get("registration_endpoint")):
        dcr_payload = _client_registration_payload(connector_row, scopes=scopes)
        response = post_json(
            _as_text(server.get("registration_endpoint")),
            dcr_payload,
            purpose="MCP OAuth dynamic client registration endpoint",
        )
        registration_client_uri = _as_text(response.get("registration_client_uri"))
        registration_access_token = _as_text(response.get("registration_access_token"))
        if registration_client_uri and registration_access_token:
            cleanup_state = {"done": False}

            def cleanup_registration() -> None:
                if cleanup_state["done"]:
                    return
                cleanup_state["done"] = True
                _delete_dynamic_client_registration(
                    registration_client_uri,
                    registration_access_token,
                )

            registration_cleanup = cleanup_registration
            register_transaction_after_rollback(conn, cleanup_registration)
        stored_client_id = _as_text(response.get("client_id"))
        if not stored_client_id:
            if registration_cleanup is not None:
                with contextlib.suppress(Exception):
                    registration_cleanup()
            raise OAuthConnectionError("Dynamic client registration did not return client_id")
        response_client_secret = response.get("client_secret")
        stored_client_secret = (
            response_client_secret if isinstance(response_client_secret, str) else ""
        )
        token_method = _as_text(response.get("token_endpoint_auth_method")) or (
            "client_secret_basic" if stored_client_secret else "none"
        )
        candidate_secrets = {
            MCP_OAUTH_CLIENT_ID_SECRET: stored_client_id,
            MCP_OAUTH_CLIENT_SECRET_SECRET: stored_client_secret,
            MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET: _as_text(
                response.get("registration_access_token")
            ),
        }
        secrets_to_save = {name: value for name, value in candidate_secrets.items() if value}
        registration = {
            "mode": "dynamic_client_registration",
            "issuer": _as_text(discovery.get("issuer")),
            "token_endpoint_auth_method": token_method,
            "registration_client_uri": registration_client_uri,
        }
    elif not registration_ready:
        raise McpOAuthManualCredentialsRequired()

    with _compensate_dynamic_registration_on_error(registration_cleanup):
        expected_row = {**connector_row, "oauth_credential_revision": expected_revision}
        with transaction_connection(conn) as transaction_conn:
            locked_row = _locked_connector_row(transaction_conn, expected_row)
            _require_current_oauth_revision(expected_row, locked_row)
            locked_metadata = _oauth_metadata_for_update(transaction_conn, locked_row)
            locked_discovery = _as_dict(locked_metadata.get("discovery"))
            if locked_discovery and (
                _as_text(locked_discovery.get("issuer")) != _as_text(discovery.get("issuer"))
                or _as_text(locked_discovery.get("resource")) != _as_text(discovery.get("resource"))
                or _as_text(locked_discovery.get("connector_endpoint"))
                != _as_text(discovery.get("connector_endpoint"))
            ):
                raise OAuthConnectionError(
                    "MCP OAuth discovery binding changed during client registration"
                )
            retained_refs = _refs_map(locked_row)
            if force_dynamic_registration:
                for secret_name in _MCP_OAUTH_APPLICATION_SECRET_NAMES:
                    obsolete_ref = retained_refs.pop(secret_name, "")
                    if obsolete_ref:
                        model_provider_store.delete_secret_ref(
                            transaction_conn,
                            account_id=account_id,
                            project_id=project_id,
                            tenant_id=tenant_id,
                            ref_key=obsolete_ref,
                        )
            saved = (
                _save_connector_oauth_secrets(
                    transaction_conn,
                    account_id=account_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    secrets_to_save=secrets_to_save,
                )
                if secrets_to_save
                else {}
            )
            refs = _merge_refs({"refs": retained_refs} if retained_refs else {}, saved)
            locked_metadata["registration"] = registration
            _update_connector_oauth(
                transaction_conn,
                connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
                updates={
                    "oauth_metadata_jsonb": locked_metadata,
                    "oauth_secret_refs_jsonb": refs,
                    "auth_status": "auth_required",
                },
            )
        metadata = locked_metadata
        connector_row["oauth_secret_refs_jsonb"] = refs
        connector_row["oauth_metadata_jsonb"] = metadata
        connector_row["auth_status"] = "auth_required"
        return {
            "mode": _as_text(registration.get("mode")),
            "client_id": stored_client_id,
            "client_secret": stored_client_secret,
            "token_endpoint_auth_method": _as_text(registration.get("token_endpoint_auth_method"))
            or ("client_secret_basic" if stored_client_secret else "none"),
        }
