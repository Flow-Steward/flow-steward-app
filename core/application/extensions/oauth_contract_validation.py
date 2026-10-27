"""Closed OAuth declaration vocabulary and validation policies."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from core.application.extensions.extension_contract_v2_common import (
    _as_text,
    _error,
    _normalize_lower,
)
from core.application.extensions.oauth_contract_models import (
    ORIGIN_LABEL_CHARSETS,
    OAuthCallbackVerification,
    OAuthCodeExchangeProfile,
    OAuthOriginBinding,
)

ALLOWED_CLIENT_AUTH_METHODS = {"client_secret_basic", "client_secret_post", "none"}


ALLOWED_CREDENTIAL_VALUE_TYPES = {"text", "secret"}


ALLOWED_CREDENTIAL_VALIDATION_TYPES = {"https_origin"}


ALLOWED_CHANGE_POLICIES = {
    "cosmetic",
    "reauthorization_required",
    "runtime_revalidate",
}


ALLOWED_AUTOCOMPLETE = {"off", "username", "current-password", "new-password"}


OAUTH_ID_MAX_LENGTH = 64


OAUTH_PARAMETER_MAX_LENGTH = 128


OAUTH_DISPLAY_MAX_LENGTH = 200


OAUTH_HELP_MAX_LENGTH = 2000


OAUTH_PLACEHOLDER_MAX_LENGTH = 500


OAUTH_ENDPOINT_MAX_LENGTH = 2048


OAUTH_CREDENTIAL_MAX_LENGTH = 4096


OAUTH_STATIC_STRING_MAX_LENGTH = 2048


OAUTH_STATIC_NUMBER_ABS_MAX = 10**15


OAUTH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


OAUTH_PARAMETER_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


OAUTH_HELP_ANCHOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


OAUTH_ENDPOINT_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


OAUTH_HELP_ANCHOR_MAX_LENGTH = 64


OAUTH_ERROR_CODES = {
    "authorization_denied",
    "invalid_client",
    "invalid_grant",
    "authorization_required",
    "rate_limited",
    "provider_unavailable",
    "malformed_response",
    "internal_failure",
}


#: Fields the exchange body always owns. A provider's static parameters may not
#: collide with them, so no declaration can replace the authorization code or the credentials.
RESERVED_CODE_EXCHANGE_PARAMETERS = {
    "client_id",
    "client_secret",
    "code",
    "code_verifier",
    "grant_type",
    "redirect_uri",
    "refresh_token",
}


RESERVED_AUTHORIZATION_PARAMETERS = {
    "client_id",
    "code_challenge",
    "code_challenge_method",
    "redirect_uri",
    "response_type",
    "scope",
    "state",
}


PROVIDER_KEYS = {
    "authorization_endpoint",
    "authorization_intents",
    "callback_id",
    "callback_verification",
    "code_exchange",
    "credential_fields",
    "description",
    "display_name",
    "error_help_anchors",
    "flow",
    "origin_binding",
    "post_connect_action_id",
    "provider_id",
    "help_page_id",
    "scope_separator",
    "setup_page_id",
    "revocation_endpoint",
    "token_endpoint",
    "unique_subject_per_configuration",
}


FLOW_KEYS = {"pkce", "type"}


CREDENTIAL_FIELD_KEYS = {
    "autocomplete",
    "browser_exposable",
    "change_policy",
    "field_id",
    "help_text",
    "label",
    "placeholder",
    "required",
    "secret",
    "validation",
    "value_type",
}


CREDENTIAL_VALIDATION_KEYS = {"max_length", "min_length", "type"}


ENDPOINT_KEYS = {"origin_binding", "path", "url"}


TOKEN_ENDPOINT_KEYS = {"client_auth_method", "origin_binding", "path", "url"}


INTENT_KEYS = {"intent_id", "static_parameters", "user_inputs"}


INTENT_INPUT_KEYS = {"input_id", "origin_binding", "parameter", "validation"}


INTENT_VALIDATION_KEYS = {"max_length", "type"}


#: PKCE modes a provider may declare. `s256` stays the default and the only value
#: an existing provider can hold; `none` exists for providers whose authorization
#: server rejects the PKCE parameters outright rather than ignoring them.
ALLOWED_PKCE_MODES = {"s256", "none"}


#: An origin binding turns one authorization input into the host that the
#: authorization and token endpoints are built from. It is deliberately not a URL
#: template: the provider declares a fixed suffix and a bounded label alphabet,
#: core assembles `https://<label><host_suffix>` and nothing else. That keeps the
#: provider's domain policy in the provider's own manifest while leaving core with
#: no provider knowledge and no expression to evaluate.
ORIGIN_BINDING_KEYS = {"binding_id", "host_suffix", "input_id", "label"}


ORIGIN_LABEL_KEYS = {"charset", "max_length", "min_length"}


ORIGIN_HOST_SUFFIX_MAX_LENGTH = 128


ORIGIN_LABEL_ABSOLUTE_MAX = 63


#: The exchange profile. Some authorization servers accept the code with
#: the client credentials alone and reject `grant_type`/`redirect_uri`; RFC 6749
#: servers require them. Both are expressible without letting a provider name an
#: arbitrary body.
CODE_EXCHANGE_KEYS = {
    "implicit_token_type",
    "include_grant_type",
    "include_redirect_uri",
    "required_response_fields",
    "static_parameters",
}


#: Token-response fields a provider may insist on. A provider that documents a
#: field as always present is better served by a loud failure than by a value
#: quietly reconstructed from somewhere else.
TOKEN_RESPONSE_FIELDS = {
    "access_token",
    "expires_in",
    "refresh_token",
    "refresh_token_expires_in",
    "scope",
}


#: The only token type this platform can use. A provider may declare that an
#: omitted `token_type` means this, and nothing else.
IMPLICIT_TOKEN_TYPES = {"bearer"}


#: Declarative verification of a signed callback.
CALLBACK_VERIFICATION_KEYS = {
    "algorithm",
    "digest",
    "message",
    "secret_source",
    "signature_parameter",
    "subject_binding",
    "subject_parameter",
}


CALLBACK_SIGNATURE_ALGORITHMS = {"hmac_sha256"}


CALLBACK_SIGNATURE_DIGESTS = {"hex"}


CALLBACK_SIGNATURE_MESSAGES = {"sorted_query_excluding_signature"}


CALLBACK_SECRET_SOURCES = {"client_secret"}


def _safe_https_url(value: Any, *, allow_private_urls: bool = False) -> bool:
    try:
        text_value = _as_text(value)
        if not text_value or len(text_value) > OAUTH_ENDPOINT_MAX_LENGTH:
            return False
        parsed = urlparse(text_value)
        hostname = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or username
        or password
        or "?" in text_value
        or "#" in text_value
        or "{" in text_value
        or "}" in text_value
    ):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    if allow_private_urls:
        return bool(hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return bool(hostname)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_canonical_https_origin(value: Any) -> bool:
    """Return whether value is an exact, canonical HTTPS browser origin."""
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not hostname
        or "%" in hostname
        or (port is not None and not 1 <= port <= 65535)
    ):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in hostname.split(".")
        ):
            return False
        canonical_host = hostname
    else:
        canonical_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    if port == 443:
        return False
    canonical_netloc = canonical_host if port is None else f"{canonical_host}:{port}"
    return value == f"https://{canonical_netloc}"


def _reject_unknown_keys(
    errors: list[dict[str, str]],
    *,
    path: str,
    value: dict[str, Any],
    allowed: set[str],
) -> None:
    for key in sorted(set(value) - allowed, key=str):
        _error(
            errors,
            path=f"{path}.{key}",
            message=f"Unknown OAuth contract key '{key}'",
            code="unknown_oauth_contract_key",
        )


def _valid_oauth_id(value: str) -> bool:
    return bool(value) and len(value) <= OAUTH_ID_MAX_LENGTH and bool(OAUTH_ID_RE.fullmatch(value))


def _bounded_copy(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= maximum else ""


def _safe_endpoint_path(value: str) -> bool:
    """Whether a bound endpoint's path is a fixed, absolute, unambiguous path.

    No query, no fragment, no traversal, no scheme-relative prefix: the host comes
    from the verified origin and the path must not be able to move the request off
    it or smuggle parameters into it.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return False
    if len(value) > OAUTH_ENDPOINT_MAX_LENGTH:
        return False
    if any(char in value for char in ("?", "#", "\\", " ")):
        return False
    segments = value.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return False
    return all(OAUTH_ENDPOINT_PATH_SEGMENT_RE.match(segment) for segment in segments if segment)


def _origin_binding(
    errors: list[dict[str, Any]], *, raw: dict[str, Any], path: str
) -> OAuthOriginBinding | None:
    """Validate the one optional dynamic-origin declaration.

    A provider may declare at most one. Everything about it is fixed data: the
    input it reads, the host suffix it appends, and a bounded label alphabet
    chosen from a closed set. No pattern travels from the manifest into a regular
    expression engine, so a manifest cannot cost anything to evaluate.
    """
    value = raw.get("origin_binding")
    if value is None:
        return None
    if not isinstance(value, dict):
        _error(
            errors,
            path=f"{path}.origin_binding",
            message="origin_binding must be an object",
            code="invalid_oauth_origin_binding",
        )
        return None
    _reject_unknown_keys(
        errors, path=f"{path}.origin_binding", value=value, allowed=ORIGIN_BINDING_KEYS
    )
    binding_id = _as_text(value.get("binding_id"))
    input_id = _as_text(value.get("input_id"))
    host_suffix = _as_text(value.get("host_suffix")).lower()
    label_value = value.get("label")
    label = label_value if isinstance(label_value, dict) else {}
    _reject_unknown_keys(
        errors, path=f"{path}.origin_binding.label", value=label, allowed=ORIGIN_LABEL_KEYS
    )
    charset = _normalize_lower(label.get("charset"))
    minimum = label.get("min_length")
    maximum = label.get("max_length")
    failed = False
    if not _valid_oauth_id(binding_id) or not _valid_oauth_id(input_id):
        _error(
            errors,
            path=f"{path}.origin_binding",
            message="origin_binding requires a binding_id and an input_id",
            code="invalid_oauth_origin_binding",
        )
        failed = True
    # The suffix has to start with a dot and be a plain hostname tail. Without the
    # leading dot, "evilexample" would satisfy a suffix of "example" and reach a
    # provider never named.
    if (
        not host_suffix.startswith(".")
        # A suffix has to name a domain, not a whole top-level namespace: binding
        # to `.example` would let any registrable name under it be addressed.
        or host_suffix.count(".") < 2
        or len(host_suffix) > ORIGIN_HOST_SUFFIX_MAX_LENGTH
        or not is_canonical_https_origin(f"https://label{host_suffix}")
    ):
        _error(
            errors,
            path=f"{path}.origin_binding.host_suffix",
            message="host_suffix must be a dotted suffix of a public hostname",
            code="invalid_oauth_origin_binding",
        )
        failed = True
    if charset not in ORIGIN_LABEL_CHARSETS:
        _error(
            errors,
            path=f"{path}.origin_binding.label.charset",
            message="Unsupported origin label charset",
            code="invalid_oauth_origin_binding",
        )
        failed = True
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not 1 <= minimum <= maximum <= ORIGIN_LABEL_ABSOLUTE_MAX
    ):
        _error(
            errors,
            path=f"{path}.origin_binding.label",
            message="origin label lengths must be 1 <= min <= max <= 63",
            code="invalid_oauth_origin_binding",
        )
        failed = True
    if failed or not isinstance(minimum, int) or not isinstance(maximum, int):
        return None
    return OAuthOriginBinding(
        binding_id=binding_id,
        input_id=input_id,
        host_suffix=host_suffix,
        charset=charset,
        min_length=minimum,
        max_length=maximum,
    )


def _code_exchange_profile(
    errors: list[dict[str, Any]], *, raw: dict[str, Any], path: str
) -> OAuthCodeExchangeProfile:
    """Validate the authorization-code exchange body profile.

    Absent, it is RFC 6749: `grant_type` and `redirect_uri` both sent. Static
    parameters are bounded strings and may not collide with a field the exchange
    already owns, so a provider cannot rewrite `code` or the client credentials.
    """
    value = raw.get("code_exchange")
    if value is None:
        return OAuthCodeExchangeProfile()
    if not isinstance(value, dict):
        _error(
            errors,
            path=f"{path}.code_exchange",
            message="code_exchange must be an object",
            code="invalid_oauth_code_exchange",
        )
        return OAuthCodeExchangeProfile()
    _reject_unknown_keys(
        errors, path=f"{path}.code_exchange", value=value, allowed=CODE_EXCHANGE_KEYS
    )
    include_grant_type = value.get("include_grant_type", True)
    include_redirect_uri = value.get("include_redirect_uri", True)
    if not isinstance(include_grant_type, bool) or not isinstance(include_redirect_uri, bool):
        _error(
            errors,
            path=f"{path}.code_exchange",
            message="code_exchange toggles must be booleans",
            code="invalid_oauth_code_exchange",
        )
        include_grant_type = True
        include_redirect_uri = True
    statics_value = value.get("static_parameters")
    statics = statics_value if isinstance(statics_value, dict) else {}
    if statics_value is not None and not isinstance(statics_value, dict):
        _error(
            errors,
            path=f"{path}.code_exchange.static_parameters",
            message="static_parameters must be an object",
            code="invalid_oauth_code_exchange",
        )
    normalized: dict[str, str] = {}
    for key, item in sorted(statics.items()):
        name = _as_text(key)
        if (
            not OAUTH_PARAMETER_RE.match(name)
            or len(name) > OAUTH_PARAMETER_MAX_LENGTH
            or name in RESERVED_CODE_EXCHANGE_PARAMETERS
        ):
            _error(
                errors,
                path=f"{path}.code_exchange.static_parameters.{name or '<invalid>'}",
                message="static exchange parameters must be safe and must not be reserved",
                code="invalid_oauth_code_exchange",
            )
            continue
        if not isinstance(item, str) or not item or len(item) > OAUTH_STATIC_STRING_MAX_LENGTH:
            _error(
                errors,
                path=f"{path}.code_exchange.static_parameters.{name}",
                message="static exchange parameter values must be bounded strings",
                code="invalid_oauth_code_exchange",
            )
            continue
        normalized[name] = item
    implicit_token_type = _normalize_lower(value.get("implicit_token_type"))
    if implicit_token_type and implicit_token_type not in IMPLICIT_TOKEN_TYPES:
        _error(
            errors,
            path=f"{path}.code_exchange.implicit_token_type",
            message="Unsupported implicit token type",
            code="invalid_oauth_code_exchange",
        )
        implicit_token_type = ""
    required_value = value.get("required_response_fields")
    required_fields: tuple[str, ...] = ()
    if required_value is not None:
        if not isinstance(required_value, list) or not all(
            isinstance(item, str) and item in TOKEN_RESPONSE_FIELDS for item in required_value
        ):
            _error(
                errors,
                path=f"{path}.code_exchange.required_response_fields",
                message="required_response_fields must name known token-response fields",
                code="invalid_oauth_code_exchange",
            )
        else:
            required_fields = tuple(sorted(dict.fromkeys(required_value)))
    return OAuthCodeExchangeProfile(
        include_grant_type=include_grant_type,
        include_redirect_uri=include_redirect_uri,
        static_parameters=normalized,
        implicit_token_type=implicit_token_type,
        required_response_fields=required_fields,
    )


def _callback_verification(
    errors: list[dict[str, Any]],
    *,
    raw: dict[str, Any],
    path: str,
    origin_binding: OAuthOriginBinding | None,
) -> OAuthCallbackVerification | None:
    """Validate the declarative signed-callback policy.

    Every value comes from a closed vocabulary, and the subject binding must name
    the provider's own origin binding, so a verified subject can only ever be the
    host the flow was started against.
    """
    value = raw.get("callback_verification")
    if value is None:
        return None
    if not isinstance(value, dict):
        _error(
            errors,
            path=f"{path}.callback_verification",
            message="callback_verification must be an object",
            code="invalid_oauth_callback_verification",
        )
        return None
    _reject_unknown_keys(
        errors,
        path=f"{path}.callback_verification",
        value=value,
        allowed=CALLBACK_VERIFICATION_KEYS,
    )
    signature_parameter = _as_text(value.get("signature_parameter"))
    subject_parameter = _as_text(value.get("subject_parameter"))
    algorithm = _normalize_lower(value.get("algorithm"))
    digest = _normalize_lower(value.get("digest"))
    message = _normalize_lower(value.get("message"))
    secret_source = _normalize_lower(value.get("secret_source"))
    subject_binding = _as_text(value.get("subject_binding"))
    failed = False
    for name, label in (
        (signature_parameter, "signature_parameter"),
        (subject_parameter, "subject_parameter"),
    ):
        if not OAUTH_PARAMETER_RE.match(name) or len(name) > OAUTH_PARAMETER_MAX_LENGTH:
            _error(
                errors,
                path=f"{path}.callback_verification.{label}",
                message=f"{label} must be a safe parameter name",
                code="invalid_oauth_callback_verification",
            )
            failed = True
    if signature_parameter and signature_parameter == subject_parameter:
        _error(
            errors,
            path=f"{path}.callback_verification",
            message="The signature and subject cannot be the same parameter",
            code="invalid_oauth_callback_verification",
        )
        failed = True
    if (
        algorithm not in CALLBACK_SIGNATURE_ALGORITHMS
        or digest not in CALLBACK_SIGNATURE_DIGESTS
        or message not in CALLBACK_SIGNATURE_MESSAGES
        or secret_source not in CALLBACK_SECRET_SOURCES
    ):
        _error(
            errors,
            path=f"{path}.callback_verification",
            message="Unsupported callback signature policy",
            code="invalid_oauth_callback_verification",
        )
        failed = True
    if origin_binding is None or subject_binding != origin_binding.binding_id:
        _error(
            errors,
            path=f"{path}.callback_verification.subject_binding",
            message="subject_binding must name the provider's origin_binding",
            code="invalid_oauth_callback_verification",
        )
        failed = True
    if failed:
        return None
    return OAuthCallbackVerification(
        signature_parameter=signature_parameter,
        algorithm=algorithm,
        digest=digest,
        message=message,
        secret_source=secret_source,
        subject_parameter=subject_parameter,
        subject_binding=subject_binding,
    )
