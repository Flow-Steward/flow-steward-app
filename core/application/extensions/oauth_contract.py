"""Generic, extension-owned OAuth provider contract validation."""

from __future__ import annotations

import json
from typing import Any

from core.application.extensions.extension_contract_v2_common import (
    _as_text,
    _error,
    _normalize_lower,
)
from core.application.extensions.oauth_contract_models import (
    DEFAULT_PKCE_MODE as DEFAULT_PKCE_MODE,
)
from core.application.extensions.oauth_contract_models import (
    DEFAULT_SCOPE_SEPARATOR as DEFAULT_SCOPE_SEPARATOR,
)
from core.application.extensions.oauth_contract_models import (
    ORIGIN_LABEL_CHARSETS as ORIGIN_LABEL_CHARSETS,
)
from core.application.extensions.oauth_contract_models import (
    SCOPE_SEPARATORS as SCOPE_SEPARATORS,
)
from core.application.extensions.oauth_contract_models import (
    OAuthAuthorizationIntent as OAuthAuthorizationIntent,
)
from core.application.extensions.oauth_contract_models import (
    OAuthCallbackVerification as OAuthCallbackVerification,
)
from core.application.extensions.oauth_contract_models import (
    OAuthCodeExchangeProfile as OAuthCodeExchangeProfile,
)
from core.application.extensions.oauth_contract_models import (
    OAuthCredentialField as OAuthCredentialField,
)
from core.application.extensions.oauth_contract_models import (
    OAuthEndpointPermission as OAuthEndpointPermission,
)
from core.application.extensions.oauth_contract_models import (
    OAuthOriginBinding as OAuthOriginBinding,
)
from core.application.extensions.oauth_contract_models import (
    OAuthProviderContract as OAuthProviderContract,
)
from core.application.extensions.oauth_contract_models import (
    provider_revocation_fingerprint as provider_revocation_fingerprint,
)
from core.application.extensions.oauth_contract_models import (
    provider_security_fingerprint as provider_security_fingerprint,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_AUTOCOMPLETE as ALLOWED_AUTOCOMPLETE,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_CHANGE_POLICIES as ALLOWED_CHANGE_POLICIES,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_CLIENT_AUTH_METHODS as ALLOWED_CLIENT_AUTH_METHODS,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_CREDENTIAL_VALIDATION_TYPES as ALLOWED_CREDENTIAL_VALIDATION_TYPES,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_CREDENTIAL_VALUE_TYPES as ALLOWED_CREDENTIAL_VALUE_TYPES,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_PKCE_MODES as ALLOWED_PKCE_MODES,
)
from core.application.extensions.oauth_contract_validation import (
    CALLBACK_SECRET_SOURCES as CALLBACK_SECRET_SOURCES,
)
from core.application.extensions.oauth_contract_validation import (
    CALLBACK_SIGNATURE_ALGORITHMS as CALLBACK_SIGNATURE_ALGORITHMS,
)
from core.application.extensions.oauth_contract_validation import (
    CALLBACK_SIGNATURE_DIGESTS as CALLBACK_SIGNATURE_DIGESTS,
)
from core.application.extensions.oauth_contract_validation import (
    CALLBACK_SIGNATURE_MESSAGES as CALLBACK_SIGNATURE_MESSAGES,
)
from core.application.extensions.oauth_contract_validation import (
    CALLBACK_VERIFICATION_KEYS as CALLBACK_VERIFICATION_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    CODE_EXCHANGE_KEYS as CODE_EXCHANGE_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    CREDENTIAL_FIELD_KEYS as CREDENTIAL_FIELD_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    CREDENTIAL_VALIDATION_KEYS as CREDENTIAL_VALIDATION_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    ENDPOINT_KEYS as ENDPOINT_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    FLOW_KEYS as FLOW_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    IMPLICIT_TOKEN_TYPES as IMPLICIT_TOKEN_TYPES,
)
from core.application.extensions.oauth_contract_validation import (
    INTENT_INPUT_KEYS as INTENT_INPUT_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    INTENT_KEYS as INTENT_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    INTENT_VALIDATION_KEYS as INTENT_VALIDATION_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_CREDENTIAL_MAX_LENGTH as OAUTH_CREDENTIAL_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_DISPLAY_MAX_LENGTH as OAUTH_DISPLAY_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_ENDPOINT_MAX_LENGTH as OAUTH_ENDPOINT_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_ENDPOINT_PATH_SEGMENT_RE as OAUTH_ENDPOINT_PATH_SEGMENT_RE,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_ERROR_CODES as OAUTH_ERROR_CODES,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_HELP_ANCHOR_MAX_LENGTH as OAUTH_HELP_ANCHOR_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_HELP_ANCHOR_RE as OAUTH_HELP_ANCHOR_RE,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_HELP_MAX_LENGTH as OAUTH_HELP_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_ID_MAX_LENGTH as OAUTH_ID_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_ID_RE as OAUTH_ID_RE,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_PARAMETER_MAX_LENGTH as OAUTH_PARAMETER_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_PARAMETER_RE as OAUTH_PARAMETER_RE,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_PLACEHOLDER_MAX_LENGTH as OAUTH_PLACEHOLDER_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_STATIC_NUMBER_ABS_MAX as OAUTH_STATIC_NUMBER_ABS_MAX,
)
from core.application.extensions.oauth_contract_validation import (
    OAUTH_STATIC_STRING_MAX_LENGTH as OAUTH_STATIC_STRING_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    ORIGIN_BINDING_KEYS as ORIGIN_BINDING_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    ORIGIN_HOST_SUFFIX_MAX_LENGTH as ORIGIN_HOST_SUFFIX_MAX_LENGTH,
)
from core.application.extensions.oauth_contract_validation import (
    ORIGIN_LABEL_ABSOLUTE_MAX as ORIGIN_LABEL_ABSOLUTE_MAX,
)
from core.application.extensions.oauth_contract_validation import (
    ORIGIN_LABEL_KEYS as ORIGIN_LABEL_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    PROVIDER_KEYS as PROVIDER_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    RESERVED_AUTHORIZATION_PARAMETERS as RESERVED_AUTHORIZATION_PARAMETERS,
)
from core.application.extensions.oauth_contract_validation import (
    RESERVED_CODE_EXCHANGE_PARAMETERS as RESERVED_CODE_EXCHANGE_PARAMETERS,
)
from core.application.extensions.oauth_contract_validation import (
    TOKEN_ENDPOINT_KEYS as TOKEN_ENDPOINT_KEYS,
)
from core.application.extensions.oauth_contract_validation import (
    TOKEN_RESPONSE_FIELDS as TOKEN_RESPONSE_FIELDS,
)
from core.application.extensions.oauth_contract_validation import (
    is_canonical_https_origin as is_canonical_https_origin,
)
from core.application.extensions.oauth_provider_compiler import (
    _compile_oauth_provider_rows,
)


def is_safe_oauth_post_connect_action(descriptor: dict[str, Any], action_id: str) -> bool:
    """Return whether an automatic post-connect action has the closed safe profile."""
    matches = [
        row
        for row in descriptor.get("action_definitions", [])
        if isinstance(row, dict) and _as_text(row.get("action_id")) == action_id
    ]
    if len(matches) != 1:
        return False
    action = matches[0]
    handler = action.get("handler")
    handler = handler if isinstance(handler, dict) else {}
    parameters = action.get("parameters")
    if (
        _as_text(handler.get("mode")).lower() != "extension"
        or action.get("mutates_platform") not in (None, False)
        or action.get("workflow_visible") not in (None, False)
        or parameters not in (None, [])
    ):
        return False
    return not any(
        isinstance(row, dict) and _as_text(row.get("operation_id")) == action_id
        for row in descriptor.get("external_effects", [])
    )


def declared_ui_page_ids(descriptor: dict[str, Any]) -> set[str]:
    ui_manifest = descriptor.get("ui_manifest")
    if not isinstance(ui_manifest, dict):
        compiled = descriptor.get("compiled_manifest")
        ui_manifest = compiled.get("ui_manifest") if isinstance(compiled, dict) else {}
    pages = ui_manifest.get("pages") if isinstance(ui_manifest, dict) else []
    if not isinstance(pages, list):
        return set()
    return {
        page_id
        for row in pages
        if isinstance(row, dict)
        if (page_id := _as_text(row.get("page_id")))
    }


def oauth_locale_copy_limits(providers: list[dict[str, Any]]) -> dict[str, int]:
    """Return exact locale keys and maximum lengths owned by OAuth declarations."""
    limits: dict[str, int] = {}
    for provider in providers:
        provider_id = _as_text(provider.get("provider_id"))
        if not provider_id:
            continue
        prefix = f"oauth.{provider_id}"
        limits[f"{prefix}.display_name"] = OAUTH_DISPLAY_MAX_LENGTH
        limits[f"{prefix}.description"] = OAUTH_HELP_MAX_LENGTH
        for field in provider.get("credential_fields") or []:
            if not isinstance(field, dict):
                continue
            field_id = _as_text(field.get("field_id"))
            if not field_id:
                continue
            field_prefix = f"{prefix}.credential.{field_id}"
            limits[f"{field_prefix}.label"] = OAUTH_DISPLAY_MAX_LENGTH
            limits[f"{field_prefix}.help_text"] = OAUTH_HELP_MAX_LENGTH
            if "placeholder" in field:
                limits[f"{field_prefix}.placeholder"] = OAUTH_PLACEHOLDER_MAX_LENGTH
    return limits


def oauth_error_help_target(provider: OAuthProviderContract, error_code: str) -> dict[str, str]:
    """Resolve provider-owned help navigation without provider copy in core."""
    result = {"page_id": provider.help_page_id}
    normalized_code = _normalize_lower(error_code)
    anchor = (
        provider.error_help_anchors.get(normalized_code)
        if normalized_code in OAUTH_ERROR_CODES
        else ""
    )
    if anchor:
        result["anchor"] = anchor
    return result


def validate_authorization_intent_inputs(
    provider: OAuthProviderContract,
    *,
    intent_id: str,
    values: dict[str, Any],
) -> dict[str, str]:
    """Map only declared, validated user inputs to provider parameters."""
    intent = provider.intents.get(intent_id)
    if intent is None:
        raise ValueError("unknown_oauth_intent")
    declarations = {_as_text(row.get("input_id")): row for row in intent.user_inputs}
    unknown = sorted(set(values) - set(declarations))
    if unknown:
        raise ValueError(f"unknown_oauth_intent_input:{unknown[0]}")

    mapped: dict[str, str] = {}
    for input_id, raw_value in values.items():
        declaration = declarations[input_id]
        value = _validated_intent_input(provider, declaration, raw_value, input_id=input_id)
        # A bound input decides where the request goes. Emitting it as a query
        # parameter as well would hand the provider a second, unverified copy of
        # the same decision, so it is deliberately not mapped.
        if _as_text(declaration.get("origin_binding")):
            continue
        mapped[_as_text(declaration.get("parameter"))] = value
    return mapped


def _is_basic_email_address(value: str) -> bool:
    parts = value.split("@")
    if len(parts) != 2:
        return False
    local_part, domain = parts
    return bool(
        local_part
        and domain
        and not any(character.isspace() for character in value)
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


def _validated_intent_input(
    provider: OAuthProviderContract,
    declaration: dict[str, Any],
    raw_value: Any,
    *,
    input_id: str,
) -> str:
    """One declared input, checked against its declared validation."""
    validation = declaration.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    value = raw_value.strip() if isinstance(raw_value, str) else ""
    max_length = int(validation.get("max_length") or 0)
    validation_type = _normalize_lower(validation.get("type"))
    valid = bool(value) and len(value) <= max_length
    if validation_type == "email":
        valid = valid and _is_basic_email_address(value)
    if validation_type == "hostname_label":
        binding = provider.origin_binding
        valid = valid and binding is not None and bool(binding.normalize(value))
    if not valid:
        raise ValueError(f"invalid_oauth_intent_input_value:{input_id}")
    return value


def resolve_authorization_origin(
    provider: OAuthProviderContract,
    *,
    intent_id: str,
    values: dict[str, Any],
) -> str:
    """The canonical origin this authorization is bound to, or "" when unbound.

    Providers without an origin binding return "" and keep their fixed endpoints.
    A provider that declares one must be given its input: an authorization with no
    origin cannot address anything, so it fails here rather than later against a
    half-built URL.
    """
    binding = provider.origin_binding
    if binding is None:
        return ""
    intent = provider.intents.get(intent_id)
    if intent is None:
        raise ValueError("unknown_oauth_intent")
    declared = {_as_text(row.get("input_id")) for row in intent.user_inputs}
    if binding.input_id not in declared:
        raise ValueError(f"invalid_oauth_intent_input_value:{binding.input_id}")
    raw = values.get(binding.input_id)
    origin = binding.normalize(raw if isinstance(raw, str) else "")
    if not origin:
        raise ValueError(f"invalid_oauth_intent_input_value:{binding.input_id}")
    return origin


def declared_oauth_callback_ids(descriptor: dict[str, Any]) -> set[str]:
    """Return only callback IDs that satisfy the reserved V1 declaration."""
    result: set[str] = set()
    for row in descriptor.get("callbacks", []):
        if not isinstance(row, dict):
            continue
        auth = row.get("auth")
        auth_type = _normalize_lower(auth.get("type")) if isinstance(auth, dict) else "none"
        auth_valid = auth is None or (isinstance(auth, dict) and auth_type == "none")
        methods = row.get("methods")
        normalized_methods = (
            [_as_text(method).upper() for method in methods] if isinstance(methods, list) else []
        )
        callback_id = _as_text(row.get("callback_id"))
        if (
            _normalize_lower(row.get("purpose")) == "oauth_authorization"
            and normalized_methods == ["GET"]
            and "connection_scoped" in row
            and row.get("connection_scoped") is False
            and not _as_text(row.get("action_id"))
            and auth_valid
            and callback_id
        ):
            result.add(callback_id)
    return result


def contract_section(descriptor: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """One Contract v2 section, wherever this descriptor happens to carry it.

    A manifest may declare a section inline or as a path to a bundle file. The
    compiler resolves the reference and publishes the rows at the descriptor's top
    level, leaving the original string in place under `extension_contract_v2` —
    so reading only the latter works for an inline declaration and silently yields
    a filename for a referenced one. Both forms are valid, and every reader has to
    accept both.
    """
    for candidate in (
        descriptor.get(section),
        _contract_root(descriptor).get(section),
    ):
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


#: Not every section may be read this way. Providers are published in a rendered
#: form that cannot be compiled again, so `compile_oauth_providers` reads the raw
#: declaration on purpose. Sections whose published and declared shapes are the
#: same — connection types among them — are safe.
CONTRACT_SECTIONS_SAFE_TO_READ_PUBLISHED: frozenset[str] = frozenset({"connection_types"})


def _contract_root(descriptor: dict[str, Any]) -> dict[str, Any]:
    direct = descriptor.get("extension_contract_v2")
    if isinstance(direct, dict):
        return direct
    runtime = descriptor.get("runtime")
    nested = runtime.get("extension_contract_v2") if isinstance(runtime, dict) else None
    return nested if isinstance(nested, dict) else {}


def compile_oauth_providers(
    descriptor: dict[str, Any], *, allow_private_urls: bool = False
) -> tuple[OAuthProviderContract, ...]:
    """Compile the public provider contract or raise a bounded validation error."""
    # Deliberately the raw declaration, not `contract_section`. The compiler
    # publishes providers at the top level in their *rendered* form — fingerprints
    # added, declaration-only keys dropped — and that payload describes a provider
    # rather than declaring one, so it cannot be compiled again.
    rows = _contract_root(descriptor).get("oauth_providers")
    rows = rows if isinstance(rows, list) else []
    callback_ids = declared_oauth_callback_ids(descriptor)
    action_ids = {
        _as_text(row.get("action_id"))
        for row in descriptor.get("action_definitions", [])
        if isinstance(row, dict) and _as_text(row.get("action_id"))
    }
    errors: list[dict[str, str]] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            _error(
                errors,
                path=f"extension_contract_v2.oauth_providers[{idx}]",
                message="OAuth provider declaration must be an object",
                code="invalid_oauth_provider",
            )
    providers = _compile_oauth_provider_rows(
        [dict(row) for row in rows if isinstance(row, dict)],
        callback_ids=callback_ids,
        action_ids=action_ids,
        validate_action_references=True,
        errors=errors,
        allow_private_urls=allow_private_urls,
    )
    for idx, provider in enumerate(providers):
        action_id = provider.post_connect_action_id
        if action_id and not is_safe_oauth_post_connect_action(descriptor, action_id):
            _error(
                errors,
                path=f"extension_contract_v2.oauth_providers[{idx}].post_connect_action_id",
                message="Post-connect actions must use the closed automatic action profile",
                code="unsafe_oauth_post_connect_action",
            )
    if errors:
        raise ValueError(json.dumps(errors, sort_keys=True))
    return tuple(providers)


def normalize_oauth_providers(
    rows: list[dict[str, Any]],
    *,
    callback_ids: set[str],
    action_ids: set[str],
    validate_action_references: bool = True,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    return [
        provider.to_dict()
        for provider in _compile_oauth_provider_rows(
            rows,
            callback_ids=callback_ids,
            action_ids=action_ids,
            validate_action_references=validate_action_references,
            errors=errors,
        )
    ]
