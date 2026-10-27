"""Compile validated OAuth provider declarations into immutable contracts."""

from __future__ import annotations

import math
from typing import Any

from core.application.extensions.extension_contract_v2_common import (
    _as_text,
    _error,
    _normalize_lower,
)
from core.application.extensions.oauth_contract_models import (
    DEFAULT_PKCE_MODE,
    DEFAULT_SCOPE_SEPARATOR,
    SCOPE_SEPARATORS,
    OAuthAuthorizationIntent,
    OAuthCredentialField,
    OAuthEndpointPermission,
    OAuthOriginBinding,
    OAuthProviderContract,
)
from core.application.extensions.oauth_contract_validation import (
    ALLOWED_AUTOCOMPLETE,
    ALLOWED_CHANGE_POLICIES,
    ALLOWED_CLIENT_AUTH_METHODS,
    ALLOWED_CREDENTIAL_VALIDATION_TYPES,
    ALLOWED_CREDENTIAL_VALUE_TYPES,
    ALLOWED_PKCE_MODES,
    CREDENTIAL_FIELD_KEYS,
    CREDENTIAL_VALIDATION_KEYS,
    ENDPOINT_KEYS,
    FLOW_KEYS,
    INTENT_INPUT_KEYS,
    INTENT_KEYS,
    INTENT_VALIDATION_KEYS,
    OAUTH_CREDENTIAL_MAX_LENGTH,
    OAUTH_DISPLAY_MAX_LENGTH,
    OAUTH_ERROR_CODES,
    OAUTH_HELP_ANCHOR_MAX_LENGTH,
    OAUTH_HELP_ANCHOR_RE,
    OAUTH_HELP_MAX_LENGTH,
    OAUTH_PARAMETER_MAX_LENGTH,
    OAUTH_PARAMETER_RE,
    OAUTH_PLACEHOLDER_MAX_LENGTH,
    OAUTH_STATIC_NUMBER_ABS_MAX,
    OAUTH_STATIC_STRING_MAX_LENGTH,
    PROVIDER_KEYS,
    RESERVED_AUTHORIZATION_PARAMETERS,
    TOKEN_ENDPOINT_KEYS,
    _bounded_copy,
    _callback_verification,
    _code_exchange_profile,
    _origin_binding,
    _reject_unknown_keys,
    _safe_endpoint_path,
    _safe_https_url,
    _valid_oauth_id,
)


def _compile_oauth_provider_rows(
    rows: list[dict[str, Any]],
    *,
    callback_ids: set[str],
    action_ids: set[str],
    validate_action_references: bool,
    errors: list[dict[str, str]],
    allow_private_urls: bool = False,
) -> list[OAuthProviderContract]:
    """Validate and normalize extension-local OAuth provider declarations."""
    normalized: list[OAuthProviderContract] = []
    seen: set[str] = set()
    for idx, raw in enumerate(rows):
        provider_error_count = len(errors)
        path = f"extension_contract_v2.oauth_providers[{idx}]"
        _reject_unknown_keys(errors, path=path, value=raw, allowed=PROVIDER_KEYS)
        provider_id_value = raw.get("provider_id")
        provider_id = provider_id_value if isinstance(provider_id_value, str) else ""
        if not _valid_oauth_id(provider_id):
            _error(
                errors,
                path=f"{path}.provider_id",
                message="provider_id must be a stable lowercase slug",
                code="invalid_oauth_identifier",
            )
        elif provider_id in seen:
            _error(
                errors,
                path=f"{path}.provider_id",
                message="provider_id must be unique",
                code="duplicate_oauth_provider",
            )
        seen.add(provider_id)

        display_name = _bounded_copy(raw.get("display_name"), maximum=OAUTH_DISPLAY_MAX_LENGTH)
        if not display_name:
            _error(
                errors,
                path=f"{path}.display_name",
                message="OAuth provider display_name is required",
                code="missing_oauth_display_name",
            )
        description = _bounded_copy(raw.get("description"), maximum=OAUTH_HELP_MAX_LENGTH)
        if not description:
            _error(
                errors,
                path=f"{path}.description",
                message="OAuth provider description is required",
                code="missing_oauth_description",
            )
        setup_page_value = raw.get("setup_page_id")
        setup_page_id = setup_page_value if isinstance(setup_page_value, str) else ""
        help_page_value = raw.get("help_page_id")
        help_page_id = help_page_value if isinstance(help_page_value, str) else ""
        for page_key, page_id in (
            ("setup_page_id", setup_page_id),
            ("help_page_id", help_page_id),
        ):
            if not _valid_oauth_id(page_id):
                _error(
                    errors,
                    path=f"{path}.{page_key}",
                    message=f"{page_key} must be a stable lowercase slug",
                    code="invalid_oauth_identifier",
                )

        error_help_value = raw.get("error_help_anchors")
        error_help_anchors: dict[str, str] = {}
        if error_help_value is not None and not isinstance(error_help_value, dict):
            _error(
                errors,
                path=f"{path}.error_help_anchors",
                message="error_help_anchors must be an object",
                code="invalid_oauth_error_help_mapping",
            )
        elif isinstance(error_help_value, dict):
            for raw_code, raw_anchor in error_help_value.items():
                code = raw_code if isinstance(raw_code, str) else ""
                anchor = raw_anchor if isinstance(raw_anchor, str) else ""
                if (
                    code not in OAUTH_ERROR_CODES
                    or not anchor
                    or len(anchor) > OAUTH_HELP_ANCHOR_MAX_LENGTH
                    or OAUTH_HELP_ANCHOR_RE.fullmatch(anchor) is None
                ):
                    _error(
                        errors,
                        path=f"{path}.error_help_anchors.{code or '<invalid>'}",
                        message="OAuth error help mapping must use a closed code and safe anchor",
                        code="invalid_oauth_error_help_mapping",
                    )
                    continue
                error_help_anchors[code] = anchor

        flow_value = raw.get("flow")
        flow = flow_value if isinstance(flow_value, dict) else {}
        _reject_unknown_keys(errors, path=f"{path}.flow", value=flow, allowed=FLOW_KEYS)
        pkce_mode = _normalize_lower(flow.get("pkce"))
        if _normalize_lower(flow.get("type")) != "authorization_code":
            _error(
                errors,
                path=f"{path}.flow",
                message="OAuth providers require the authorization_code flow",
                code="invalid_oauth_flow",
            )
        if pkce_mode not in ALLOWED_PKCE_MODES:
            _error(
                errors,
                path=f"{path}.flow.pkce",
                message="OAuth providers must declare PKCE as s256 or none",
                code="invalid_oauth_flow",
            )
            pkce_mode = DEFAULT_PKCE_MODE

        scope_separator = _normalize_lower(raw.get("scope_separator")) or DEFAULT_SCOPE_SEPARATOR
        if scope_separator not in SCOPE_SEPARATORS:
            _error(
                errors,
                path=f"{path}.scope_separator",
                message="scope_separator must be space or comma",
                code="invalid_oauth_scope_separator",
            )
            scope_separator = DEFAULT_SCOPE_SEPARATOR

        unique_subject = raw.get("unique_subject_per_configuration")
        if unique_subject is not None and not isinstance(unique_subject, bool):
            _error(
                errors,
                path=f"{path}.unique_subject_per_configuration",
                message="unique_subject_per_configuration must be a boolean",
                code="invalid_oauth_unique_subject",
            )
            unique_subject = False

        origin_binding = _origin_binding(errors, raw=raw, path=path)
        code_exchange = _code_exchange_profile(errors, raw=raw, path=path)

        credential_fields: list[OAuthCredentialField] = []
        credential_field_ids: set[str] = set()
        credential_rows = raw.get("credential_fields")
        if not isinstance(credential_rows, list):
            _error(
                errors, path=f"{path}.credential_fields", message="credential_fields must be a list"
            )
            credential_rows = []
        if not credential_rows:
            _error(
                errors,
                path=f"{path}.credential_fields",
                message="At least one OAuth credential field is required",
                code="missing_oauth_credential_field",
            )
        for field_idx, field_raw in enumerate(credential_rows):
            field_path = f"{path}.credential_fields[{field_idx}]"
            field = field_raw if isinstance(field_raw, dict) else {}
            _reject_unknown_keys(
                errors,
                path=field_path,
                value=field,
                allowed=CREDENTIAL_FIELD_KEYS,
            )
            field_id_value = field.get("field_id")
            field_id = field_id_value if isinstance(field_id_value, str) else ""
            value_type = _normalize_lower(field.get("value_type"))
            change_policy = _normalize_lower(field.get("change_policy"))
            required_value = field.get("required")
            secret_value = field.get("secret")
            browser_exposable_value = field.get("browser_exposable", False)
            if not _valid_oauth_id(field_id):
                _error(
                    errors,
                    path=f"{field_path}.field_id",
                    message="credential field_id must be a stable lowercase slug",
                    code="invalid_oauth_identifier",
                )
            elif field_id in credential_field_ids:
                _error(
                    errors,
                    path=f"{field_path}.field_id",
                    message=f"Duplicate OAuth credential field '{field_id}'",
                    code="duplicate_oauth_credential_field",
                )
            credential_field_ids.add(field_id)
            field_semantics_valid = True
            if value_type not in ALLOWED_CREDENTIAL_VALUE_TYPES:
                field_semantics_valid = False
            if not isinstance(required_value, bool) or not isinstance(secret_value, bool):
                field_semantics_valid = False
            if isinstance(secret_value, bool) and secret_value != (value_type == "secret"):
                field_semantics_valid = False
            if not isinstance(browser_exposable_value, bool) or (
                browser_exposable_value is True and secret_value is True
            ):
                field_semantics_valid = False
            if not field_semantics_valid:
                _error(
                    errors,
                    path=field_path,
                    message="credential field type and boolean semantics are invalid",
                    code="invalid_oauth_credential_field",
                )
            if change_policy not in ALLOWED_CHANGE_POLICIES:
                _error(
                    errors,
                    path=f"{field_path}.change_policy",
                    message="unsupported change policy",
                    code="invalid_oauth_credential_field",
                )
            label = _bounded_copy(field.get("label"), maximum=OAUTH_DISPLAY_MAX_LENGTH)
            help_text = _bounded_copy(field.get("help_text"), maximum=OAUTH_HELP_MAX_LENGTH)
            placeholder_value = field.get("placeholder")
            placeholder = ""
            if placeholder_value is not None:
                placeholder = _bounded_copy(placeholder_value, maximum=OAUTH_PLACEHOLDER_MAX_LENGTH)
            autocomplete_value = field.get("autocomplete")
            autocomplete = (
                _normalize_lower(autocomplete_value) if autocomplete_value is not None else ""
            )
            validation_value = field.get("validation")
            validation = validation_value if isinstance(validation_value, dict) else {}
            _reject_unknown_keys(
                errors,
                path=f"{field_path}.validation",
                value=validation,
                allowed=CREDENTIAL_VALIDATION_KEYS,
            )
            min_length = validation.get("min_length")
            max_length = validation.get("max_length")
            validation_type_value = validation.get("type")
            validation_type = _normalize_lower(validation_type_value)
            validation_valid = (
                isinstance(min_length, int)
                and not isinstance(min_length, bool)
                and isinstance(max_length, int)
                and not isinstance(max_length, bool)
                and 0 <= min_length <= max_length <= OAUTH_CREDENTIAL_MAX_LENGTH
                and max_length >= 1
                and (
                    validation_type_value is None
                    or (
                        isinstance(validation_type_value, str)
                        and validation_type in ALLOWED_CREDENTIAL_VALIDATION_TYPES
                    )
                )
            )
            ui_valid = bool(label and help_text)
            if placeholder_value is not None and not placeholder:
                ui_valid = False
            if autocomplete and autocomplete not in ALLOWED_AUTOCOMPLETE:
                ui_valid = False
            if not ui_valid or not validation_valid:
                _error(
                    errors,
                    path=field_path,
                    message="credential field UI metadata and validation are invalid",
                    code="invalid_oauth_credential_field",
                )
            normalized_validation: dict[str, Any] = {
                "min_length": min_length if isinstance(min_length, int) else 0,
                "max_length": max_length if isinstance(max_length, int) else 0,
            }
            if validation_type_value is not None:
                normalized_validation["type"] = validation_type
            credential_fields.append(
                OAuthCredentialField(
                    field_id=field_id,
                    value_type=value_type,
                    required=required_value is True,
                    secret=secret_value is True,
                    change_policy=change_policy,
                    label=label,
                    help_text=help_text,
                    placeholder=placeholder,
                    autocomplete=autocomplete,
                    validation=normalized_validation,
                    browser_exposable=browser_exposable_value is True,
                )
            )

        def endpoint(
            name: str,
            *,
            required: bool,
            token: bool = False,
            provider_row: dict[str, Any] = raw,
            provider_path: str = path,
            origin_binding: OAuthOriginBinding | None = origin_binding,
        ) -> OAuthEndpointPermission | None:
            value = provider_row.get(name)
            if value is not None and not isinstance(value, dict):
                _error(
                    errors,
                    path=f"{provider_path}.{name}",
                    message=f"{name} must be an object",
                    code="invalid_oauth_endpoint",
                )
            row = value if isinstance(value, dict) else {}
            _reject_unknown_keys(
                errors,
                path=f"{provider_path}.{name}",
                value=row,
                allowed=TOKEN_ENDPOINT_KEYS if token else ENDPOINT_KEYS,
            )
            url_value = row.get("url")
            url = url_value.strip() if isinstance(url_value, str) else ""
            binding_name = _as_text(row.get("origin_binding"))
            endpoint_path = _as_text(row.get("path"))
            auth_method = _normalize_lower(row.get("client_auth_method")) if token else ""
            if token and auth_method not in ALLOWED_CLIENT_AUTH_METHODS:
                _error(
                    errors,
                    path=f"{provider_path}.{name}.client_auth_method",
                    message=f"Unsupported OAuth client auth method '{auth_method}'",
                    code="unsupported_oauth_client_auth_method",
                )
            # Exactly one addressing form. Accepting both would leave two answers
            # to "where does this request go", and the loser would be silent.
            if url and binding_name:
                _error(
                    errors,
                    path=f"{provider_path}.{name}",
                    message="An endpoint declares either url or origin_binding, never both",
                    code="invalid_oauth_endpoint",
                )
                return None
            if binding_name:
                if origin_binding is None or binding_name != origin_binding.binding_id:
                    _error(
                        errors,
                        path=f"{provider_path}.{name}.origin_binding",
                        message="origin_binding must name the provider's origin_binding",
                        code="invalid_oauth_endpoint",
                    )
                    return None
                if not _safe_endpoint_path(endpoint_path):
                    _error(
                        errors,
                        path=f"{provider_path}.{name}.path",
                        message="A bound endpoint requires a fixed absolute path",
                        code="unsafe_oauth_endpoint",
                    )
                    return None
                return OAuthEndpointPermission(
                    client_auth_method=auth_method,
                    origin_binding=binding_name,
                    path=endpoint_path,
                )
            if endpoint_path:
                _error(
                    errors,
                    path=f"{provider_path}.{name}.path",
                    message="path is only allowed with origin_binding",
                    code="invalid_oauth_endpoint",
                )
                return None
            if required and not url:
                _error(
                    errors,
                    path=f"{provider_path}.{name}",
                    message=f"{name} is required",
                )
            if url and not _safe_https_url(url, allow_private_urls=allow_private_urls):
                _error(
                    errors,
                    path=f"{provider_path}.{name}.url",
                    message="OAuth endpoints must use an absolute HTTPS URL",
                    code="unsafe_oauth_endpoint",
                )
            if not url:
                return None
            return OAuthEndpointPermission(url=url, client_auth_method=auth_method)

        authorization_endpoint = endpoint("authorization_endpoint", required=True)
        token_endpoint = endpoint("token_endpoint", required=True, token=True)
        revocation_endpoint = endpoint("revocation_endpoint", required=False)
        callback_verification = _callback_verification(
            errors, raw=raw, path=path, origin_binding=origin_binding
        )
        # A declared binding that no endpoint uses is a mistake worth naming: the
        # operator would be asked for a value that changes nothing.
        if origin_binding is not None and not any(
            row is not None and row.is_bound
            for row in (authorization_endpoint, token_endpoint, revocation_endpoint)
        ):
            _error(
                errors,
                path=f"{path}.origin_binding",
                message="A declared origin_binding must be used by at least one endpoint",
                code="invalid_oauth_origin_binding",
            )
        client_id_fields = [field for field in credential_fields if field.field_id == "client_id"]
        if len(client_id_fields) != 1:
            _error(
                errors,
                path=f"{path}.credential_fields",
                message="OAuth providers require exactly one client_id credential field",
                code="missing_oauth_client_id",
            )
        elif (
            client_id_fields[0].value_type != "text"
            or client_id_fields[0].secret
            or not client_id_fields[0].required
            or client_id_fields[0].change_policy != "reauthorization_required"
        ):
            _error(
                errors,
                path=f"{path}.credential_fields",
                message="client_id must be a required non-secret text field",
                code="invalid_oauth_credential_field",
            )
        if token_endpoint:
            client_secret_fields = [
                field for field in credential_fields if field.field_id == "client_secret"
            ]
            undeclared_secret_roles = [
                field
                for field in credential_fields
                if field.secret and field.field_id != "client_secret"
            ]
            if undeclared_secret_roles:
                _error(
                    errors,
                    path=f"{path}.credential_fields",
                    message="Secret credentials must use the client_secret role",
                    code="invalid_oauth_credential_field",
                )
            if token_endpoint.client_auth_method in {
                "client_secret_basic",
                "client_secret_post",
            }:
                if len(client_secret_fields) != 1:
                    _error(
                        errors,
                        path=f"{path}.credential_fields",
                        message="Secret client authentication requires client_secret",
                        code="missing_oauth_client_secret",
                    )
                elif (
                    client_secret_fields[0].value_type != "secret"
                    or not client_secret_fields[0].secret
                    or not client_secret_fields[0].required
                    or client_secret_fields[0].change_policy == "cosmetic"
                ):
                    _error(
                        errors,
                        path=f"{path}.token_endpoint.client_auth_method",
                        message="client_secret must be a required secret field",
                        code="incompatible_oauth_client_auth_method",
                    )
            if token_endpoint.client_auth_method == "none" and (
                client_secret_fields or undeclared_secret_roles
            ):
                _error(
                    errors,
                    path=f"{path}.token_endpoint.client_auth_method",
                    message="Public clients cannot declare a client secret field",
                    code="incompatible_oauth_client_auth_method",
                )

        callback_id_value = raw.get("callback_id")
        callback_id = callback_id_value if isinstance(callback_id_value, str) else ""
        if not _valid_oauth_id(callback_id):
            _error(
                errors,
                path=f"{path}.callback_id",
                message="callback_id must be a stable lowercase slug",
                code="invalid_oauth_identifier",
            )
        elif callback_id not in callback_ids:
            _error(
                errors,
                path=f"{path}.callback_id",
                message=f"OAuth callback '{callback_id}' is not declared by this extension",
                code="unknown_oauth_callback",
            )
        post_connect_value = raw.get("post_connect_action_id")
        post_connect_action_id = (
            post_connect_value
            if isinstance(post_connect_value, str) and post_connect_value
            else None
        )
        if post_connect_value is not None and post_connect_action_id is None:
            _error(
                errors,
                path=f"{path}.post_connect_action_id",
                message="post_connect_action_id must be a stable lowercase slug",
                code="invalid_oauth_identifier",
            )
        if post_connect_action_id and not _valid_oauth_id(post_connect_action_id):
            _error(
                errors,
                path=f"{path}.post_connect_action_id",
                message="post_connect_action_id must be a stable lowercase slug",
                code="invalid_oauth_identifier",
            )
        elif (
            post_connect_action_id
            and validate_action_references
            and post_connect_action_id not in action_ids
        ):
            _error(
                errors,
                path=f"{path}.post_connect_action_id",
                message=f"Post-connect action '{post_connect_action_id}' is not declared",
                code="unknown_oauth_post_connect_action",
            )

        intents: list[OAuthAuthorizationIntent] = []
        intent_ids: set[str] = set()
        intent_rows = raw.get("authorization_intents", [])
        if not isinstance(intent_rows, list):
            _error(
                errors,
                path=f"{path}.authorization_intents",
                message="authorization_intents must be a list",
            )
            intent_rows = []
        if not intent_rows:
            _error(
                errors,
                path=f"{path}.authorization_intents",
                message="At least one authorization intent is required",
                code="missing_oauth_intent",
            )
        for intent_idx, intent_raw in enumerate(intent_rows):
            intent = intent_raw if isinstance(intent_raw, dict) else {}
            _reject_unknown_keys(
                errors,
                path=f"{path}.authorization_intents[{intent_idx}]",
                value=intent,
                allowed=INTENT_KEYS,
            )
            intent_id_value = intent.get("intent_id")
            intent_id = intent_id_value if isinstance(intent_id_value, str) else ""
            if not _valid_oauth_id(intent_id):
                _error(
                    errors,
                    path=f"{path}.authorization_intents[{intent_idx}].intent_id",
                    message="intent_id must be a stable lowercase slug",
                    code="invalid_oauth_identifier",
                )
            elif intent_id in intent_ids:
                _error(
                    errors,
                    path=f"{path}.authorization_intents[{intent_idx}].intent_id",
                    message=f"Duplicate OAuth intent '{intent_id}'",
                    code="duplicate_oauth_intent",
                )
            intent_ids.add(intent_id)
            static_parameters = intent.get("static_parameters")
            user_inputs = intent.get("user_inputs")
            if not isinstance(static_parameters, dict):
                _error(
                    errors,
                    path=f"{path}.authorization_intents[{intent_idx}].static_parameters",
                    message="static_parameters must be an object",
                    code="invalid_oauth_intent_parameter",
                )
                static_parameters = {}
            for parameter in static_parameters:
                parameter_value = static_parameters[parameter]
                if not isinstance(parameter, str):
                    _error(
                        errors,
                        path=f"{path}.authorization_intents[{intent_idx}].static_parameters",
                        message="Authorization parameter names must be strings",
                        code="invalid_oauth_intent_parameter",
                    )
                elif parameter in RESERVED_AUTHORIZATION_PARAMETERS:
                    _error(
                        errors,
                        path=(
                            f"{path}.authorization_intents[{intent_idx}]"
                            f".static_parameters.{parameter}"
                        ),
                        message=f"Authorization parameter '{parameter}' is owned by the platform",
                        code="reserved_oauth_parameter",
                    )
                elif len(
                    parameter
                ) > OAUTH_PARAMETER_MAX_LENGTH or not OAUTH_PARAMETER_RE.fullmatch(parameter):
                    _error(
                        errors,
                        path=f"{path}.authorization_intents[{intent_idx}].static_parameters",
                        message=f"Authorization parameter '{parameter}' is invalid",
                        code="invalid_oauth_intent_parameter",
                    )
                scalar_valid = False
                if isinstance(parameter_value, bool):
                    scalar_valid = True
                elif isinstance(parameter_value, str):
                    scalar_valid = len(parameter_value) <= OAUTH_STATIC_STRING_MAX_LENGTH
                elif isinstance(parameter_value, int):
                    scalar_valid = abs(parameter_value) <= OAUTH_STATIC_NUMBER_ABS_MAX
                elif isinstance(parameter_value, float):
                    scalar_valid = (
                        math.isfinite(parameter_value)
                        and abs(parameter_value) <= OAUTH_STATIC_NUMBER_ABS_MAX
                    )
                if not scalar_valid:
                    _error(
                        errors,
                        path=(
                            f"{path}.authorization_intents[{intent_idx}]"
                            f".static_parameters.{parameter}"
                        ),
                        message=f"Authorization parameter '{parameter}' has an invalid value",
                        code="invalid_oauth_intent_parameter",
                    )
            input_ids: set[str] = set()
            mapped_parameters = {
                parameter for parameter in static_parameters if isinstance(parameter, str)
            }
            if user_inputs is None:
                user_inputs = []
            elif not isinstance(user_inputs, list):
                _error(
                    errors,
                    path=f"{path}.authorization_intents[{intent_idx}].user_inputs",
                    message="user_inputs must be a list",
                    code="invalid_oauth_intent_input",
                )
                user_inputs = []
            if isinstance(user_inputs, list):
                for input_idx, input_raw in enumerate(user_inputs):
                    input_row = input_raw if isinstance(input_raw, dict) else {}
                    _reject_unknown_keys(
                        errors,
                        path=(
                            f"{path}.authorization_intents[{intent_idx}].user_inputs[{input_idx}]"
                        ),
                        value=input_row,
                        allowed=INTENT_INPUT_KEYS,
                    )
                    input_id_value = input_row.get("input_id")
                    input_id = input_id_value if isinstance(input_id_value, str) else ""
                    parameter_value = input_row.get("parameter")
                    parameter = parameter_value if isinstance(parameter_value, str) else ""
                    validation = input_row.get("validation")
                    if isinstance(validation, dict):
                        _reject_unknown_keys(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}].validation"
                            ),
                            value=validation,
                            allowed=INTENT_VALIDATION_KEYS,
                        )
                    if not _valid_oauth_id(input_id):
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}].input_id"
                            ),
                            message="input_id must be a stable lowercase slug",
                            code="invalid_oauth_identifier",
                        )
                    # An input either becomes an authorization query parameter or
                    # becomes the origin the endpoints are built on. Never both:
                    # the value that decides where the request goes must not also
                    # be reflected into the request it addresses.
                    bound = _as_text(input_row.get("origin_binding"))
                    if bound and parameter:
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}]"
                            ),
                            message="An intent input maps to a parameter or an origin_binding, not both",
                            code="invalid_oauth_intent_input",
                        )
                    elif bound:
                        if origin_binding is None or bound != origin_binding.binding_id:
                            _error(
                                errors,
                                path=(
                                    f"{path}.authorization_intents[{intent_idx}]"
                                    f".user_inputs[{input_idx}].origin_binding"
                                ),
                                message="origin_binding must name the provider's origin_binding",
                                code="invalid_oauth_intent_input",
                            )
                        elif origin_binding.input_id != input_id:
                            _error(
                                errors,
                                path=(
                                    f"{path}.authorization_intents[{intent_idx}]"
                                    f".user_inputs[{input_idx}].input_id"
                                ),
                                message="The bound input must be the origin_binding's input_id",
                                code="invalid_oauth_intent_input",
                            )
                        if (
                            not isinstance(validation, dict)
                            or _normalize_lower(validation.get("type")) != "hostname_label"
                            or not isinstance(validation.get("max_length"), int)
                            or isinstance(validation.get("max_length"), bool)
                            or not 1 <= int(validation.get("max_length") or 0) <= 255
                        ):
                            _error(
                                errors,
                                path=(
                                    f"{path}.authorization_intents[{intent_idx}]"
                                    f".user_inputs[{input_idx}].validation"
                                ),
                                message="A bound input requires hostname_label validation",
                                code="invalid_oauth_intent_input",
                            )
                    elif (
                        not parameter
                        or len(parameter) > OAUTH_PARAMETER_MAX_LENGTH
                        or not OAUTH_PARAMETER_RE.fullmatch(parameter)
                        or not isinstance(validation, dict)
                        or _normalize_lower(validation.get("type")) not in {"email", "text"}
                        or not isinstance(validation.get("max_length"), int)
                        or isinstance(validation.get("max_length"), bool)
                        or not 1 <= int(validation.get("max_length") or 0) <= 2048
                    ):
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}]"
                            ),
                            message="Intent inputs require an id, parameter, and closed validation",
                            code="invalid_oauth_intent_input",
                        )
                    if input_id in input_ids:
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}].input_id"
                            ),
                            message=f"Duplicate OAuth intent input '{input_id}'",
                            code="duplicate_oauth_intent_input",
                        )
                    input_ids.add(input_id)
                    if not parameter:
                        continue
                    if parameter in RESERVED_AUTHORIZATION_PARAMETERS:
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}].parameter"
                            ),
                            message=f"Authorization parameter '{parameter}' is owned by the platform",
                            code="reserved_oauth_parameter",
                        )
                    elif parameter in mapped_parameters:
                        _error(
                            errors,
                            path=(
                                f"{path}.authorization_intents[{intent_idx}]"
                                f".user_inputs[{input_idx}].parameter"
                            ),
                            message=f"Authorization parameter '{parameter}' is mapped more than once",
                            code="duplicate_oauth_intent_parameter",
                        )
                    mapped_parameters.add(parameter)
            intents.append(
                OAuthAuthorizationIntent(
                    intent_id=intent_id,
                    static_parameters=dict(static_parameters),
                    user_inputs=tuple(dict(row) for row in user_inputs if isinstance(row, dict))
                    if isinstance(user_inputs, list)
                    else (),
                )
            )

        if authorization_endpoint and token_endpoint and len(errors) == provider_error_count:
            normalized.append(
                OAuthProviderContract(
                    provider_id=provider_id,
                    display_name=display_name,
                    description=description,
                    setup_page_id=setup_page_id,
                    help_page_id=help_page_id,
                    error_help_anchors=error_help_anchors,
                    credential_fields=tuple(credential_fields),
                    authorization_endpoint=authorization_endpoint,
                    token_endpoint=token_endpoint,
                    revocation_endpoint=revocation_endpoint,
                    callback_id=callback_id,
                    authorization_intents=tuple(intents),
                    post_connect_action_id=post_connect_action_id,
                    pkce=pkce_mode,
                    scope_separator=scope_separator,
                    origin_binding=origin_binding,
                    code_exchange=code_exchange,
                    callback_verification=callback_verification,
                    unique_subject_per_configuration=bool(unique_subject),
                )
            )
    return normalized
