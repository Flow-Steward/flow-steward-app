"""MCP OAuth resource, issuer, scope and credential binding policies."""

from __future__ import annotations

import json
import re
import time
import urllib.error
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import parse_http_list

from core.application.auth import oauth_client
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.extensions import url_safety
from core.application.management.mcp_connector_common import (
    _normalize_json_object,
    _normalize_string_list,
)
from core.application.management.mcp_connector_http import (
    _connector_rpc_url,
)

_MESSAGE_MCP_OAUTH_CONNECTOR_ENDPOINT_CHANGED_DURING_AUTHORIZATION_RECONNECT = (
    "MCP OAuth connector endpoint changed during authorization; reconnect required"
)


_MESSAGE_OAUTH_STATE_IS_NOT_FOR_AN_MCP_CONNECTOR = "OAuth state is not for an MCP connector"


_MESSAGE_OAUTH_STATE_HAS_EXPIRED_OR_WAS_ALREADY_USED = "OAuth state has expired or was already used"


_MESSAGE_OAUTH_STATE_DOES_NOT_MATCH_THE_STORED_MCP_CONNECTOR = (
    "OAuth state does not match the stored MCP connector"
)


MCP_OAUTH_STATE_TYPE = "mcp_connector"


MCP_OAUTH_ACCESS_TOKEN_SECRET = "_oauth_access_token"  # pragma: allowlist secret


MCP_OAUTH_CLIENT_ID_SECRET = "_oauth_client_id"  # pragma: allowlist secret


MCP_OAUTH_CLIENT_SECRET_SECRET = "_oauth_client_secret"  # pragma: allowlist secret


MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET = (
    "_oauth_registration_access_token"  # pragma: allowlist secret
)


MCP_OAUTH_REFRESH_TOKEN_SECRET = oauth_client.OAUTH_REFRESH_TOKEN_SECRET


MCP_OAUTH_EXPIRES_AT_SECRET = oauth_client.OAUTH_EXPIRES_AT_SECRET


MCP_OAUTH_GRANTED_SCOPES_SECRET = oauth_client.OAUTH_GRANTED_SCOPES_SECRET


MCP_OAUTH_TOKEN_TYPE_SECRET = oauth_client.OAUTH_TOKEN_TYPE_SECRET


MCP_OAUTH_REFRESH_SKEW_SECONDS = oauth_client.OAUTH_REFRESH_SKEW_SECONDS


_OAUTH_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


_SENSITIVE_ERROR_FIELD_PATTERN = re.compile(
    r"(?i)\b(refresh_token|client_secret|access_token|code_verifier|authorization)"
    r"([\"']?\s*[:=]\s*[\"']?)[^&\s,\"'}]+"
)


_BEARER_ERROR_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Z0-9._~+/\-=]+")


_MCP_OAUTH_APPLICATION_SECRET_NAMES = frozenset(
    {
        MCP_OAUTH_CLIENT_ID_SECRET,
        MCP_OAUTH_CLIENT_SECRET_SECRET,
        MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    }
)


class McpOAuthManualCredentialsRequired(OAuthConnectionError):
    """Stable signal that discovery succeeded but manual app credentials are required."""

    reason_code = "manual_credentials_required"

    def __init__(self) -> None:
        super().__init__("This authorization server requires client credentials.")


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_string_list(value: Any) -> list[str]:
    return _normalize_string_list(value)


def _connector_lineage(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    connector_id = _as_text(row.get("connector_id"))
    account_id = _as_text(row.get("account_id"))
    project_id = _as_text(row.get("project_id"))
    tenant_id = _as_text(row.get("tenant_id"))
    if not connector_id or not account_id or not project_id or not tenant_id:
        raise OAuthConnectionError("MCP connector account, project, and tenant lineage is required")
    return connector_id, account_id, project_id, tenant_id


def _now() -> float:
    return time.time()


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _assert_https_unless_dev(url: str, *, purpose: str) -> None:
    oauth_client._assert_https_unless_dev(url, purpose=purpose)


def canonical_mcp_resource_uri(base_url: str) -> str:
    parsed = urlsplit(_connector_rpc_url({"transport": "streamable_http", "base_url": base_url}))
    scheme = parsed.scheme.lower()
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    netloc = hostname
    if parsed.port and not (
        (scheme == "https" and parsed.port == 443) or (scheme == "http" and parsed.port == 80)
    ):
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or ""
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _normalized_resource_uri(value: str) -> str:
    try:
        return canonical_mcp_resource_uri(value)
    except Exception:
        return _as_text(value).rstrip("/")


def _current_connector_endpoint(connector_row: dict[str, Any]) -> str:
    return _normalized_resource_uri(_as_text(connector_row.get("base_url")))


def _root_mcp_resource_uri(base_url: str) -> str:
    parsed = urlsplit(canonical_mcp_resource_uri(base_url))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _validated_mcp_resource(resource: Any, *, expected_resource: str) -> str:
    candidate = _as_text(resource)
    if not candidate:
        raise OAuthConnectionError("MCP protected resource metadata must include resource")
    if urlsplit(candidate).fragment:
        raise OAuthConnectionError(
            "MCP protected resource metadata resource must not include a fragment"
        )
    try:
        normalized_resource = canonical_mcp_resource_uri(candidate)
        normalized_expected = canonical_mcp_resource_uri(expected_resource)
    except Exception as exc:
        raise OAuthConnectionError(
            "MCP protected resource metadata resource must be an absolute http(s) URI"
        ) from exc
    if normalized_resource != normalized_expected:
        raise OAuthConnectionError(
            "MCP protected resource metadata resource must exactly match the expected MCP resource"
        )
    return normalized_resource


def protected_resource_metadata_urls(base_url: str) -> list[str]:
    parsed = urlsplit(canonical_mcp_resource_uri(base_url))
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    urls: list[str] = []
    endpoint_path = parsed.path.lstrip("/")
    if endpoint_path or parsed.query:
        path_suffix = f"/{endpoint_path}" if endpoint_path else ""
        query_suffix = f"?{parsed.query}" if parsed.query else ""
        urls.append(f"{origin}/.well-known/oauth-protected-resource{path_suffix}{query_suffix}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return list(dict.fromkeys(urls))


def _protected_resource_metadata_candidates(
    base_url: str,
    *,
    challenge_resource_metadata: str,
) -> list[dict[str, str]]:
    endpoint_resource = canonical_mcp_resource_uri(base_url)
    root_resource = _root_mcp_resource_uri(base_url)
    candidates: list[dict[str, str]] = []
    if challenge_resource_metadata:
        candidates.append(
            {
                "url": challenge_resource_metadata,
                "expected_resource": endpoint_resource,
                "source": "www_authenticate",
            }
        )
        return candidates
    urls = protected_resource_metadata_urls(base_url)
    for index, url in enumerate(urls):
        candidates.append(
            {
                "url": url,
                "expected_resource": endpoint_resource
                if index == 0 and url != urls[-1]
                else root_resource,
                "source": "well_known_endpoint"
                if index == 0 and url != urls[-1]
                else "well_known_root",
            }
        )
    return candidates


def authorization_server_metadata_urls(issuer: str) -> list[str]:
    parsed = urlsplit(_validate_metadata_url(issuer, purpose="OAuth issuer"))
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    issuer_path = parsed.path.strip("/")
    if issuer_path:
        return [
            f"{origin}/.well-known/oauth-authorization-server/{issuer_path}",
            f"{origin}/.well-known/openid-configuration/{issuer_path}",
            f"{origin}/{issuer_path}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration",
    ]


def _validate_metadata_url(url: str, *, purpose: str) -> str:
    value = _as_text(url)
    if not value:
        raise OAuthConnectionError(f"{purpose} is required")
    try:
        _assert_https_unless_dev(value, purpose=purpose)
        safe_url = url_safety.assert_safe_remote_http_url(value, purpose=purpose)
        parsed = urlparse(safe_url)
        if parsed.scheme.lower() == "http":
            url_safety.resolve_pinned_ips(
                str(parsed.hostname or ""),
                port=int(parsed.port or 80),
                purpose=purpose,
                require_private=True,
            )
        return safe_url
    except Exception as exc:
        if isinstance(exc, OAuthConnectionError):
            raise
        raise OAuthConnectionError(str(exc)) from exc


def _sanitize_oauth_error_message(message: Any) -> str:
    text = _as_text(message)
    text = _SENSITIVE_ERROR_FIELD_PATTERN.sub(r"\1\2[redacted]", text)
    text = _BEARER_ERROR_PATTERN.sub(r"\1[redacted]", text)
    return text[:1000]


def parse_www_authenticate(value: str | list[str] | tuple[str, ...] | None) -> list[dict[str, str]]:
    raw_values = list(value) if isinstance(value, (list, tuple)) else [str(value or "")]
    challenges: list[dict[str, str]] = []
    for raw in raw_values:
        for item in parse_http_list(str(raw or "")):
            text = item.strip()
            if not text:
                continue
            if text.lower().startswith("bearer "):
                challenges.append({"scheme": "bearer", **_parse_challenge_params(text[7:])})
            elif challenges and "=" in text:
                challenges[-1].update(_parse_challenge_params(text))
            else:
                scheme = text.split(None, 1)[0].strip().lower()
                if scheme:
                    challenges.append({"scheme": scheme})
    return challenges


def _parse_challenge_params(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in parse_http_list(value):
        key, sep, raw = item.partition("=")
        if not sep:
            continue
        normalized_key = key.strip().lower()
        normalized_value = raw.strip()
        if len(normalized_value) >= 2 and normalized_value[:1] == normalized_value[-1:] == '"':
            normalized_value = normalized_value[1:-1]
        if normalized_key:
            params[normalized_key] = normalized_value
    return params


def _bearer_challenge(value: str | list[str] | tuple[str, ...] | None) -> dict[str, str]:
    for challenge in parse_www_authenticate(value):
        if challenge.get("scheme") == "bearer":
            return challenge
    return {}


def _oauth_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return _normalize_json_object(row.get("oauth_metadata_jsonb") or row.get("oauth_metadata"))


def _registration_issuer_binding_error(
    registration: dict[str, Any],
    *,
    discovery: dict[str, Any],
) -> str:
    if not registration:
        return ""
    registration_mode = _as_text(registration.get("mode")) or "manual"
    if registration_mode == "client_id_metadata_document":
        return ""
    discovery_issuer = _as_text(discovery.get("issuer"))
    if not discovery_issuer:
        return ""
    registration_issuer = _as_text(registration.get("issuer"))
    if _issuer_identifiers_match(registration_issuer, discovery_issuer):
        return ""
    return (
        "MCP OAuth client credentials are bound to a different authorization server; "
        "reconnect required"
    )


def _oauth_binding_error(
    connector_row: dict[str, Any],
    *,
    metadata: dict[str, Any],
    values: dict[str, str],
) -> str:
    discovery = _as_dict(metadata.get("discovery"))
    registration = _as_dict(metadata.get("registration"))
    registration_problem = _registration_issuer_binding_error(
        registration,
        discovery=discovery,
    )
    if registration_problem:
        return registration_problem
    if not _as_text(values.get(MCP_OAUTH_ACCESS_TOKEN_SECRET)) and not _as_text(
        values.get(MCP_OAUTH_REFRESH_TOKEN_SECRET)
    ):
        return ""
    token = _as_dict(metadata.get("token"))
    current_endpoint = _current_connector_endpoint(connector_row)
    bound_endpoint = _as_text(token.get("connector_endpoint")) or _as_text(
        discovery.get("connector_endpoint")
    )
    if bound_endpoint and _normalized_resource_uri(bound_endpoint) != current_endpoint:
        return "MCP OAuth credentials are bound to a different resource; reconnect required"
    discovery_resource = _as_text(discovery.get("resource"))
    token_resource = _as_text(token.get("resource"))
    if (
        token_resource
        and discovery_resource
        and _normalized_resource_uri(token_resource) != _normalized_resource_uri(discovery_resource)
    ):
        return "MCP OAuth credentials are bound to a different resource; reconnect required"
    discovery_issuer = _as_text(discovery.get("issuer"))
    token_issuer = _as_text(token.get("issuer"))
    if (
        token_issuer
        and discovery_issuer
        and not _issuer_identifiers_match(token_issuer, discovery_issuer)
    ):
        return (
            "MCP OAuth tokens were issued by a different authorization server; reconnect required"
        )
    return ""


def _is_oauth_binding_error_message(message: Any) -> bool:
    text = _as_text(message)
    return (
        "different resource" in text
        or "different authorization server" in text
        or "were issued by a different authorization server" in text
        or "credential revision changed" in text
    )


def _oauth_secret_refs(row: dict[str, Any]) -> dict[str, Any]:
    return _normalize_json_object(
        row.get("oauth_secret_refs_jsonb") or row.get("oauth_secret_refs")
    )


def _refs_map(row: dict[str, Any]) -> dict[str, str]:
    refs = _oauth_secret_refs(row).get("refs")
    if not isinstance(refs, dict):
        return {}
    return {
        _as_text(name): _as_text(ref)
        for name, ref in refs.items()
        if _as_text(name) and _as_text(ref)
    }


def _secret_ref_key(connector_id: str, secret_name: str) -> str:
    normalized_connector_id = _as_text(connector_id)
    normalized_name = _as_text(secret_name)
    if not normalized_connector_id:
        raise OAuthConnectionError("MCP connector_id is required for OAuth secret refs")
    if not normalized_name:
        raise OAuthConnectionError("MCP OAuth secret name is required")
    return f"mcp_connector:{normalized_connector_id}:{normalized_name}"


def _merge_refs(existing: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    refs = dict(_as_dict(existing.get("refs")))
    refs.update(_as_dict(saved.get("refs")))
    return {"refs": refs} if refs else {}


def _oauth_credential_revision(row: Mapping[str, Any]) -> int:
    try:
        return max(1, int(row.get("oauth_credential_revision") or 1))
    except (TypeError, ValueError):
        return 1


def _require_current_oauth_revision(
    expected_row: Mapping[str, Any],
    locked_row: Mapping[str, Any],
) -> None:
    if _oauth_credential_revision(expected_row) != _oauth_credential_revision(locked_row):
        raise OAuthConnectionError(
            "MCP OAuth credentials changed (credential revision changed); "
            "refresh connector state and retry"
        )


def _manual_token_endpoint_auth_method(
    server: dict[str, Any],
    *,
    has_client_secret: bool,
) -> str:
    advertised_methods = _as_string_list(server.get("token_endpoint_auth_methods_supported"))
    if not advertised_methods:
        return "client_secret_basic" if has_client_secret else "none"
    if has_client_secret and "client_secret_post" in advertised_methods:
        return "client_secret_post"
    if has_client_secret and "client_secret_basic" in advertised_methods:
        return "client_secret_basic"
    if not has_client_secret and "none" in advertised_methods:
        return "none"
    raise OAuthConnectionError(
        "OAuth authorization server does not support client_secret_basic or client_secret_post"
    )


def _issuer_identifiers_match(actual: str, expected: str) -> bool:
    actual_value = _as_text(actual)
    expected_value = _as_text(expected)
    if actual_value == expected_value:
        return True
    actual_url = urlsplit(actual_value)
    expected_url = urlsplit(expected_value)
    # RFC 9728 deployments in the wild can publish a root authorization-server
    # identifier with or without the syntactic trailing slash. Limit the
    # compatibility exception to that root-only spelling so issuer mix-up
    # defenses remain exact for paths, queries, fragments, and authorities.
    return (
        actual_url.scheme == expected_url.scheme
        and actual_url.netloc == expected_url.netloc
        and actual_url.path in {"", "/"}
        and expected_url.path in {"", "/"}
        and not actual_url.query
        and not expected_url.query
        and not actual_url.fragment
        and not expected_url.fragment
    )


def _validate_authorization_server_metadata(
    metadata: dict[str, Any],
    *,
    expected_issuer: str = "",
) -> dict[str, Any]:
    issuer = _as_text(metadata.get("issuer"))
    if expected_issuer and not _issuer_identifiers_match(issuer, expected_issuer):
        raise OAuthConnectionError(
            "OAuth authorization server metadata issuer must match discovery issuer"
        )
    authorization_endpoint = _as_text(metadata.get("authorization_endpoint"))
    token_endpoint = _as_text(metadata.get("token_endpoint"))
    if not authorization_endpoint or not token_endpoint:
        raise OAuthConnectionError("OAuth authorization server metadata is missing endpoints")
    _validate_metadata_url(authorization_endpoint, purpose="OAuth authorization endpoint")
    _validate_metadata_url(token_endpoint, purpose="OAuth token endpoint")
    revocation_endpoint = _as_text(metadata.get("revocation_endpoint"))
    if revocation_endpoint:
        _validate_metadata_url(revocation_endpoint, purpose="OAuth revocation endpoint")
    methods = _as_string_list(metadata.get("code_challenge_methods_supported"))
    if "S256" not in methods:
        raise OAuthConnectionError("OAuth authorization server must advertise PKCE S256 support")
    return dict(metadata)


def _scopes_for_start(
    discovery: dict[str, Any],
    requested_scopes: list[str] | None = None,
) -> list[str]:
    selected = _as_string_list(discovery.get("challenge_scope"))
    for scope in _as_string_list(requested_scopes or []):
        if scope not in selected:
            selected.append(scope)
    # MCP authorization, Scope Selection Strategy: with no challenge scope and
    # nothing configured, request the resource's scopes_supported (the minimal
    # set it needs); configured scopes are never widened to that list.
    if not selected:
        selected = _as_string_list(discovery.get("scopes_supported"))
    return selected


def _client_secret_for_token(
    values: dict[str, str],
    *,
    auth_config: dict[str, Any],
) -> str:
    method = _as_text(auth_config.get("token_endpoint_auth_method")).lower()
    if method == "none":
        return ""
    value = values.get(MCP_OAUTH_CLIENT_SECRET_SECRET)
    return value if isinstance(value, str) else ""


def _oauth_client_auth(
    data: dict[str, str],
    *,
    method: str,
    client_id: str,
    client_secret: str,
) -> tuple[dict[str, str], tuple[str, str] | None]:
    normalized_method = _as_text(method).lower() or "none"
    payload = dict(data)
    if normalized_method == "none":
        if client_id:
            payload["client_id"] = client_id
        return payload, None
    if normalized_method == "client_secret_basic":
        if not client_id or not client_secret:
            raise OAuthConnectionError("OAuth client_secret_basic requires client credentials")
        return payload, (client_id, client_secret)
    if normalized_method == "client_secret_post":
        if not client_id or not client_secret:
            raise OAuthConnectionError("OAuth client_secret_post requires client credentials")
        payload["client_id"] = client_id
        payload["client_secret"] = client_secret
        return payload, None
    raise OAuthConnectionError(f"Unsupported token endpoint auth method: {normalized_method}")


def _www_auth_from_error(exc: Exception) -> str:
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, urllib.error.HTTPError):
        return _as_text(cause.headers.get("WWW-Authenticate"))
    return ""


def _http_status_from_error(exc: Exception) -> int:
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, urllib.error.HTTPError):
        return int(cause.code)
    return 0
