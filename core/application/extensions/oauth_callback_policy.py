from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from core.application.extensions.oauth_contract import (
    OAUTH_ID_MAX_LENGTH,
    OAUTH_ID_RE,
    OAuthProviderContract,
    compile_oauth_providers,
    contract_section,
    declared_ui_page_ids,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_post_connect import (
    OAuthPostConnectResult,
)
from core.application.management.project_connection_secrets import connection_secret_ref_key
from core.infrastructure.security.hmac_signatures import (
    hmac_digest_matches,
    hmac_sha256_hex,
)

_MESSAGE_INVALID_SCOPE = "invalid scope"


_MESSAGE_STALE_INSTALL = "stale install"


_MESSAGE_STALE_CONFIGURATION = "stale configuration"


_MESSAGE_STALE_CONNECTION = "stale connection"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _scopes(
    value: Any, *, fallback: list[str], provider: OAuthProviderContract | None = None
) -> list[str]:
    if value is None or value == "":
        # A provider that documents `scope` as always present has already had the
        # response rejected as malformed by the transport. Falling back here would
        # record a grant the provider never confirmed, so it is refused rather
        # than reconstructed.
        if provider is not None and "scope" in provider.code_exchange.required_response_fields:
            raise ValueError(_MESSAGE_INVALID_SCOPE)
        # RFC 6749 token responses may omit scope; then the grant is the exact
        # requested scope snapshot, not a provider-specific guess.
        return sorted(dict.fromkeys(fallback))
    if not isinstance(value, str):
        raise ValueError(_MESSAGE_INVALID_SCOPE)
    # A provider that joins scopes with commas reads them back the same way.
    parsed = list(provider.split_scopes(value)) if provider is not None else value.split()
    if not parsed or any(not item or len(item) > 512 for item in parsed):
        raise ValueError(_MESSAGE_INVALID_SCOPE)
    return sorted(dict.fromkeys(parsed))


def _state_origin(state: Mapping[str, Any]) -> str:
    """The origin this authorization was bound to when it started.

    It is read back from the state rather than from the callback, so a caller
    cannot move the token request to a host of their choosing after the fact.
    """
    payload = _mapping(state.get("intent_payload"))
    return _text(payload.get("authorization_origin"))


def _connection_owned_refs(connection_id: str, secret_refs: Any) -> dict[str, str] | None:
    if secret_refs in (None, {}):
        return {}
    if not isinstance(secret_refs, Mapping):
        return None
    raw_refs = secret_refs.get("refs")
    if raw_refs is None:
        return {}
    if not isinstance(raw_refs, Mapping):
        return None
    validated: dict[str, str] = {}
    for raw_name, raw_ref in raw_refs.items():
        name = _text(raw_name)
        ref = _text(raw_ref)
        if not name or not ref:
            return None
        try:
            expected_ref = connection_secret_ref_key(connection_id, name)
        except ValueError:
            return None
        if not secrets.compare_digest(ref, expected_ref):
            return None
        validated[name] = ref
    return validated


def _provider_error_outcome(
    value: Any,
) -> Literal["authorization_denied", "restart_required", "provider_unavailable"]:
    code = _text(value).lower()
    if code in {"access_denied", "authorization_denied"}:
        return "authorization_denied"
    if code in {"server_error", "temporarily_unavailable"}:
        return "provider_unavailable"
    return "restart_required"


def _callback_url_matches(redirect_uri: Any, *, public_base_url: Any, callback_path: Any) -> bool:
    expected_path = _text(callback_path)
    if not expected_path.startswith("/") or "?" in expected_path or "#" in expected_path:
        return False
    expected_url = f"{_text(public_base_url).rstrip('/')}{expected_path}"
    try:
        parsed = urlsplit(_text(redirect_uri))
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path == expected_path
        and _text(redirect_uri) == expected_url
    )


_VERIFIED_IDENTITY_FIELDS = (
    "subject",
    "verified_display_name",
    "verified_email",
    "safe_metadata",
)


def _post_connect_config(
    connection: Mapping[str, Any], result: OAuthPostConnectResult
) -> tuple[dict[str, Any], str]:
    config = _mapping(connection.get("connection_config"))
    oauth = _mapping(config.get("oauth"))
    oauth["lifecycle_status"] = "connected" if result.health == "ok" else result.health
    oauth["token_type"] = "Bearer"
    oauth["last_error_code"] = result.error_code
    display_name = _text(connection.get("display_name"))
    if result.health == "ok":
        oauth.update(
            {
                "subject": result.subject,
                "verified_display_name": result.verified_display_name,
                "verified_email": result.verified_email,
                "safe_metadata": dict(result.safe_metadata),
            }
        )
        display_name = result.verified_display_name or result.verified_email or result.subject
    else:
        for field in _VERIFIED_IDENTITY_FIELDS:
            oauth.pop(field, None)
    return {**config, "oauth": oauth}, display_name


def _post_connect_outcome(
    result: OAuthPostConnectResult,
) -> Literal["connected", "needs_attention", "authorization_required"]:
    if result.health == "ok":
        return "connected"
    return result.health


def _operational_state(*, ready: bool, error_code: str | None = None) -> dict[str, str | bool]:
    """Project connection state derived only from the closed OAuth result."""
    health_status = "healthy" if ready else "unknown"
    if not ready and error_code:
        health_status = "failed"
    return {
        "health_status": health_status,
        "runtime_status": "ready" if ready else "unavailable",
        "last_error": error_code or "",
        "mark_last_seen_ok": ready,
    }


def _return_page_id(state: dict[str, Any], descriptor: Mapping[str, Any]) -> str | None:
    value = _text(_mapping(state.get("intent_payload")).get("return_page_id"))
    if (
        not value
        or len(value) > OAUTH_ID_MAX_LENGTH
        or OAUTH_ID_RE.fullmatch(value) is None
        or value not in declared_ui_page_ids(dict(descriptor))
    ):
        return None
    return value


def _provider(descriptor: dict[str, Any], provider_id: str) -> OAuthProviderContract | None:
    try:
        providers = compile_oauth_providers(descriptor)
    except ValueError:
        return None
    return next((row for row in providers if row.provider_id == provider_id), None)


def _required_connection_scopes(
    descriptor: dict[str, Any], *, connection_type: str, provider_id: str
) -> set[str] | None:
    # Accepts a section declared inline or resolved from a bundle file. Reading
    # only the declared place returns a filename for a referenced section, no
    # row matches, and the caller reads that as "this connection type requires
    # nothing knowable" — which is refused, indistinguishably from a real
    # scope shortfall.
    matches = [
        row
        for row in contract_section(descriptor, "connection_types")
        if _text(row.get("connection_type_id")) == connection_type
    ]
    if len(matches) != 1:
        return None
    auth = _mapping(matches[0].get("auth"))
    scopes = _mapping(auth.get("scopes"))
    required = scopes.get("required")
    if (
        _text(auth.get("type")).lower() != "oauth2"
        or _text(auth.get("provider_id")) != provider_id
        or not isinstance(required, list)
    ):
        return None
    normalized = {_text(item) for item in required if isinstance(item, str) and _text(item)}
    return normalized if len(normalized) == len(required) else None


def _repository_scope(state: dict[str, Any]) -> dict[str, str]:
    return {
        key: _text(state.get(key))
        for key in (
            "tenant_id",
            "account_id",
            "project_id",
            "extension_install_id",
            "extension_id",
            "provider_id",
            "configuration_id",
        )
    }


def _configuration_lineage_matches(
    configuration: dict[str, Any], state: dict[str, Any], provider: OAuthProviderContract
) -> bool:
    exact = {
        "configuration_id": _text(state.get("configuration_id")),
        "tenant_id": _text(state.get("tenant_id")),
        "account_id": _text(state.get("account_id")),
        "project_id": _text(state.get("project_id")),
        "extension_install_id": _text(state.get("extension_install_id")),
        "extension_id": _text(state.get("extension_id")),
        "extension_release_id": _text(state.get("extension_release_id")),
        "provider_id": _text(state.get("provider_id")),
    }
    if any(_text(configuration.get(key)) != expected for key, expected in exact.items()):
        return False
    try:
        revisions_match = int(configuration.get("configuration_revision") or 0) == int(
            state.get("configuration_revision") or 0
        ) and int(configuration.get("authorization_revision") or 0) == int(
            state.get("authorization_revision") or 0
        )
    except (TypeError, ValueError):
        return False
    return bool(
        revisions_match
        and _text(configuration.get("provider_security_fingerprint"))
        == provider_security_fingerprint(provider)
        and _text(configuration.get("provider_revocation_fingerprint"))
        == provider_revocation_fingerprint(provider)
    )


def _configuration_matches(
    configuration: dict[str, Any], state: dict[str, Any], provider: OAuthProviderContract
) -> bool:
    return bool(
        _configuration_lineage_matches(
            configuration,
            state,
            provider,
        )
        and _text(configuration.get("status")) in {"ready_for_authorization", "verified"}
    )


def _connection_matches(
    connection: dict[str, Any], state: dict[str, Any], *, expected_revision: int
) -> bool:
    exact = {
        "connection_id": _text(state.get("existing_connection_id")),
        "tenant_id": _text(state.get("tenant_id")),
        "account_id": _text(state.get("account_id")),
        "project_id": _text(state.get("project_id")),
        "extension_install_id": _text(state.get("extension_install_id")),
        "extension_id": _text(state.get("extension_id")),
        "oauth_provider_id": _text(state.get("provider_id")),
        "oauth_configuration_id": _text(state.get("configuration_id")),
        "oauth_extension_release_id": _text(state.get("extension_release_id")),
        "oauth_provider_security_fingerprint": _text(state.get("provider_security_fingerprint")),
        "connection_type": _text(state.get("connection_type")),
    }
    try:
        revision = int(connection.get("credential_revision") or 0)
    except (TypeError, ValueError):
        return False
    return revision == expected_revision and all(
        _text(connection.get(key)) == expected for key, expected in exact.items()
    )


def _callback_signature_ok(
    *,
    provider: OAuthProviderContract,
    client_secret: str,
    state: Mapping[str, Any],
    query_pairs: tuple[tuple[str, str], ...],
) -> bool:
    """Whether a signed callback proves itself, and names the expected subject.

    Providers that declare no verification policy are unchanged: they return
    True without inspecting anything. For the rest both checks must hold —
    the signature over the canonical message, and the subject matching the
    origin this authorization was bound to when it started.
    """
    policy = provider.callback_verification
    if policy is None:
        return True
    if not client_secret or not query_pairs:
        return False
    provided = ""
    subject_raw = ""
    for key, value in query_pairs:
        if key == policy.signature_parameter:
            provided = value
        elif key == policy.subject_parameter:
            subject_raw = value
    if not provided or not subject_raw:
        return False
    binding = provider.origin_binding
    if binding is None:
        return False
    # The subject is normalized through the provider's own domain policy, so
    # a callback cannot smuggle in a host shaped differently from the one the
    # flow was started against.
    subject = binding.normalize(subject_raw)
    if not subject or subject != _state_origin(state):
        return False
    message = "&".join(
        f"{key}={value}" for key, value in sorted(query_pairs) if key != policy.signature_parameter
    )
    expected = hmac_sha256_hex(secret=client_secret, payload=message.encode("utf-8"))
    return hmac_digest_matches(expected, provided)
