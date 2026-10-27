from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.application.extensions.extension_projection_v2 import render_oauth_provider_setup
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    oauth_error_help_target,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
    validate_authorization_intent_inputs,
)
from core.infrastructure.extension_runtime.extension_runtime import (
    compile_extension_bundle,
    validate_compiled_manifest,
)


def _provider() -> dict:
    return {
        "provider_id": "example",
        "display_name": "Example Identity",
        "description": "Connect an Example account.",
        "setup_page_id": "oauth_setup",
        "help_page_id": "oauth_help",
        "error_help_anchors": {
            "invalid_client": "client-credentials",
            "authorization_required": "authorize-again",
        },
        "flow": {"type": "authorization_code", "pkce": "s256"},
        "credential_fields": [
            {
                "field_id": "client_id",
                "value_type": "text",
                "required": True,
                "secret": False,
                "change_policy": "reauthorization_required",
                "label": "Client ID",
                "help_text": "Enter the OAuth application client ID.",
                "placeholder": "example-client-id",
                "autocomplete": "username",
                "validation": {"min_length": 1, "max_length": 512},
            },
            {
                "field_id": "client_secret",
                "value_type": "secret",
                "required": True,
                "secret": True,
                "change_policy": "runtime_revalidate",
                "label": "Client secret",
                "help_text": "Enter the OAuth application client secret.",
                "autocomplete": "current-password",
                "validation": {"min_length": 1, "max_length": 4096},
            },
        ],
        "authorization_endpoint": {"url": "https://identity.example.test/authorize"},
        "token_endpoint": {
            "url": "https://identity.example.test/token",
            "client_auth_method": "client_secret_post",
        },
        "revocation_endpoint": {"url": "https://identity.example.test/revoke"},
        "callback_id": "oauth_authorization",
        "authorization_intents": [
            {
                "intent_id": "connect",
                "static_parameters": {"access_type": "offline"},
                "user_inputs": [
                    {
                        "input_id": "email_hint",
                        "parameter": "login_hint",
                        "validation": {"type": "email", "max_length": 320},
                    }
                ],
            }
        ],
        "post_connect_action_id": "verify_identity",
    }


def _descriptor(provider: dict | None = None) -> dict:
    return {
        "extension_id": "fixture.oauth-provider",
        "callbacks": [
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": ["GET"],
                "connection_scoped": False,
            }
        ],
        "action_definitions": [
            {
                "action_id": "verify_identity",
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "workflow_visible": False,
                "parameters": [],
            }
        ],
        "ui_manifest": {
            "version": "1",
            "pages": [
                {"page_id": "oauth_setup", "title": "OAuth setup", "components": []},
                {"page_id": "oauth_help", "title": "OAuth help", "components": []},
            ],
        },
        "runtime": {
            "extension_contract_v2": {
                "oauth_providers": [provider or _provider()],
                "connection_types": [
                    {
                        "connection_type_id": "example_mail",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "example",
                            "scopes": {"required": ["mail.read"], "optional": ["mail.write"]},
                        },
                    },
                    {
                        "connection_type_id": "example_files",
                        "auth": {
                            "type": "oauth2",
                            "provider_id": "example",
                            "scopes": {"required": ["files.read"], "optional": []},
                        },
                    },
                ],
            }
        },
    }


def _compiled_manifest(callback: dict) -> dict:
    return {
        "manifest_version": 2,
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "manifest_path": "/tmp/extension.yaml",
        "kind": "tool_provider",
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "healthcheck": {"command": ["python3", "health.py"]},
        "ui_manifest_ref": "ui/ui_manifest.yaml",
        "query_definitions": [],
        "action_definitions": [{"action_id": "ingress_event", "handler": {"mode": "extension"}}],
        "callbacks": [callback],
        "ui_manifest": {"version": "1", "pages": []},
    }


def _error_codes(result: dict) -> set[str]:
    return {str(item.get("code") or "") for item in result.get("errors", [])}


def test_provider_local_oauth_contract_compiles_without_provider_specific_core_logic() -> None:
    result = validate_extension_contract_v2(_descriptor())

    assert result["valid"] is True, result["errors"]
    providers = result["normalized_contract"]["oauth_providers"]
    assert len(providers) == 1
    assert providers[0]["provider_id"] == "example"
    assert providers[0]["callback_id"] == "oauth_authorization"
    assert providers[0]["post_connect_action_id"] == "verify_identity"
    assert providers[0]["setup_page_id"] == "oauth_setup"
    assert providers[0]["help_page_id"] == "oauth_help"
    assert providers[0]["credential_fields"][0]["label"] == "Client ID"
    assert providers[0]["credential_fields"][0]["validation"] == {
        "min_length": 1,
        "max_length": 512,
    }
    assert len(providers[0]["security_fingerprint"]) == 64
    assert len(providers[0]["revocation_fingerprint"]) == 64
    assert result["preview"]["resolved_ids"]["oauth_provider_ids"] == ["example"]

    compiled = compile_oauth_providers(_descriptor())
    assert compiled[0].token_auth_method == "client_secret_post"
    assert set(compiled[0].intents) == {"connect"}
    assert provider_security_fingerprint(compiled[0]) == providers[0]["security_fingerprint"]
    assert provider_revocation_fingerprint(compiled[0]) == providers[0]["revocation_fingerprint"]


def test_oauth_error_help_mapping_is_manifest_local_closed_and_cosmetic() -> None:
    compiled = compile_oauth_providers(_descriptor())[0]
    assert oauth_error_help_target(compiled, "invalid_client") == {
        "page_id": "oauth_help",
        "anchor": "client-credentials",
    }
    assert oauth_error_help_target(compiled, "provider_unavailable") == {"page_id": "oauth_help"}
    assert oauth_error_help_target(compiled, "unknown-provider-copy") == {"page_id": "oauth_help"}

    changed = copy.deepcopy(_provider())
    changed["error_help_anchors"]["invalid_client"] = "new-safe-anchor"
    assert provider_security_fingerprint(compile_oauth_providers(_descriptor(changed))[0]) == (
        provider_security_fingerprint(compiled)
    )


@pytest.mark.parametrize(
    "mapping",
    [
        {"vendor_raw_error": "help"},
        {"invalid_client": "../unsafe"},
        {"invalid_client": "x" * 65},
        ["invalid_client"],
    ],
)
def test_oauth_error_help_mapping_rejects_unknown_codes_and_unsafe_anchors(mapping) -> None:
    provider = _provider()
    provider["error_help_anchors"] = mapping
    result = validate_extension_contract_v2(_descriptor(provider))
    assert result["valid"] is False
    assert "invalid_oauth_error_help_mapping" in _error_codes(result)


def test_oauth_setup_projection_uses_extension_locale_copy_and_safe_fallbacks() -> None:
    result = validate_extension_contract_v2(_descriptor())
    provider = result["normalized_contract"]["oauth_providers"][0]

    projected = render_oauth_provider_setup(
        provider=provider,
        locale="ru",
        locale_bundles={
            "en": {
                "oauth.example.credential.client_secret.label": "Client secret EN",
            },
            "ru": {
                "oauth.example.display_name": "Пример OAuth",
                "oauth.example.credential.client_id.label": "Идентификатор клиента",
                "oauth.example.credential.client_id.help_text": "Подсказка расширения",
                "oauth.example.credential.client_id.placeholder": "идентификатор",
            },
        },
    )

    assert projected["display_name"] == "Пример OAuth"
    assert projected["setup_page_id"] == "oauth_setup"
    assert projected["help_page_id"] == "oauth_help"
    assert projected["credential_fields"][0]["label"] == "Идентификатор клиента"
    assert projected["credential_fields"][0]["help_text"] == "Подсказка расширения"
    assert projected["credential_fields"][0]["placeholder"] == "идентификатор"
    assert projected["credential_fields"][1]["label"] == "Client secret EN"
    assert projected["credential_fields"][1]["help_text"] == (
        "Enter the OAuth application client secret."
    )


@pytest.mark.parametrize("page_key", ["setup_page_id", "help_page_id"])
def test_bundle_compiler_resolves_oauth_setup_and_help_pages(tmp_path: Path, page_key: str) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"
    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0][page_key] = "missing_page"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = compile_extension_bundle(bundle)

    assert result["status"] == "invalid"
    error_codes = {
        str(item.get("code") or "")
        for item in result["validation_errors"] + result["compile_errors"]
    }
    assert "unknown_oauth_ui_page" in error_codes


def test_bundle_compiler_accepts_resolved_extension_owned_oauth_pages() -> None:
    bundle = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"

    result = compile_extension_bundle(bundle)

    assert result["status"] == "valid", result["validation_errors"] + result["compile_errors"]
    assert result["account_scope"]["scope_type"] == "project"
    oauth_setup_page = next(
        page for page in result["ui_manifest"]["pages"] if page["page_id"] == "oauth_setup"
    )
    assert oauth_setup_page["project_scoped"] is True
    compiled = result["compiled_manifest"]
    assert compiled["oauth_providers"][0]["setup_page_id"] == "oauth_setup"
    projected = render_oauth_provider_setup(
        provider=compiled["oauth_providers"][0],
        locale="ru",
        locale_bundles=compiled["locale_bundles"],
    )
    assert projected["display_name"] == "Пример OAuth"
    assert projected["credential_fields"][0]["label"] == "Идентификатор клиента"


def test_bundle_compiler_rejects_oauth_help_anchor_missing_from_declared_page(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"
    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]["error_help_anchors"][
        "invalid_client"
    ] = "missing-help-target"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = compile_extension_bundle(bundle)

    assert result["status"] == "invalid"
    assert "unknown_oauth_help_anchor" in {
        str(item.get("code") or "")
        for item in result["validation_errors"] + result["compile_errors"]
    }


def test_bundle_compiler_accepts_a_setup_page_that_also_connects_accounts(
    tmp_path: Path,
) -> None:
    """One page is both the provider's setup page and its connection form page.

    The browser shows the app configuration with the Connect form below it on such a
    page, so the compiler must not reject the layout (File Transfer's Dropbox page).
    The split layout, a setup page without a form, is covered by the test above.
    """
    source = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"
    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    manifest_path = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]["setup_page_id"] = "mail"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = compile_extension_bundle(bundle)

    assert result["status"] == "valid", result["validation_errors"] + result["compile_errors"]
    assert result["compiled_manifest"]["oauth_providers"][0]["setup_page_id"] == "mail"


@pytest.mark.parametrize(
    "locale_key",
    [
        "oauth.example.display_name",
        "oauth.example.description",
        "oauth.example.credential.client_id.label",
        "oauth.example.credential.client_id.help_text",
        "oauth.example.credential.client_id.placeholder",
    ],
)
def test_bundle_compiler_rejects_oversized_localized_oauth_copy(
    tmp_path: Path, locale_key: str
) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"
    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    locale_path = bundle / "locales" / "ru.yaml"
    locale_bundle = yaml.safe_load(locale_path.read_text(encoding="utf-8"))
    locale_bundle[locale_key] = "x" * 3000
    locale_path.write_text(yaml.safe_dump(locale_bundle, allow_unicode=True), encoding="utf-8")

    result = compile_extension_bundle(bundle)

    assert result["status"] == "invalid"
    assert "invalid_oauth_locale_value" in {
        str(item.get("code") or "") for item in result["validation_errors"]
    }


def test_oauth_setup_projection_never_renders_oversized_locale_copy() -> None:
    result = validate_extension_contract_v2(_descriptor())
    provider = result["normalized_contract"]["oauth_providers"][0]
    projected = render_oauth_provider_setup(
        provider=provider,
        locale="ru",
        locale_bundles={
            "ru": {
                "oauth.example.display_name": "x" * 201,
                "oauth.example.description": "x" * 2001,
                "oauth.example.credential.client_id.label": "x" * 201,
                "oauth.example.credential.client_id.help_text": "x" * 2001,
                "oauth.example.credential.client_id.placeholder": "x" * 501,
            }
        },
    )

    assert projected["display_name"] == provider["display_name"]
    assert projected["description"] == provider["description"]
    assert projected["credential_fields"][0]["label"] == "Client ID"
    assert projected["credential_fields"][0]["help_text"] == (
        "Enter the OAuth application client ID."
    )
    assert projected["credential_fields"][0]["placeholder"] == "example-client-id"


@pytest.mark.parametrize("missing_key", ["label", "help_text", "validation"])
def test_credential_fields_require_extension_owned_ui_and_validation(missing_key: str) -> None:
    provider = _provider()
    provider["credential_fields"][0].pop(missing_key)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "invalid_oauth_credential_field" in _error_codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        {"validation": {"min_length": 0, "max_length": 512, "regex": ".*"}},
        {"validation": {"min_length": 1, "max_length": 512, "type": "url"}},
        {"validation": {"min_length": -1, "max_length": 512}},
        {"validation": {"min_length": 513, "max_length": 512}},
        {"validation": {"min_length": 1, "max_length": 4097}},
        {"autocomplete": "provider-specific-token"},
    ],
)
def test_credential_ui_and_validation_contract_is_closed_and_bounded(mutation: dict) -> None:
    provider = _provider()
    provider["credential_fields"][0].update(mutation)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert _error_codes(result) & {
        "unknown_oauth_contract_key",
        "invalid_oauth_credential_field",
    }


def test_credential_copy_is_cosmetic_but_validation_is_security_critical() -> None:
    baseline = compile_oauth_providers(_descriptor())[0]
    cosmetic_provider = _provider()
    cosmetic_provider["credential_fields"][0].update(
        {
            "label": "Application identifier",
            "help_text": "Different extension-owned instructions.",
            "placeholder": "different-placeholder",
            "autocomplete": "off",
        }
    )
    cosmetic_provider["setup_page_id"] = "oauth_help"
    cosmetic_provider["help_page_id"] = "oauth_setup"
    cosmetic = compile_oauth_providers(_descriptor(cosmetic_provider))[0]
    validation_provider = _provider()
    validation_provider["credential_fields"][0]["validation"]["max_length"] = 256
    validation = compile_oauth_providers(_descriptor(validation_provider))[0]

    assert provider_security_fingerprint(cosmetic) == provider_security_fingerprint(baseline)
    assert provider_security_fingerprint(validation) != provider_security_fingerprint(baseline)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda provider: provider.update({"provider_id": "a" * 65}),
        lambda provider: provider.update({"display_name": "x" * 201}),
        lambda provider: provider.update({"description": "x" * 2001}),
        lambda provider: provider.update({"setup_page_id": "a" * 65}),
        lambda provider: provider["credential_fields"][0].update({"label": "x" * 201}),
        lambda provider: provider["credential_fields"][0].update({"help_text": "x" * 2001}),
        lambda provider: provider["authorization_endpoint"].update(
            {"url": "https://example.test/" + "x" * 2030}
        ),
        lambda provider: provider["authorization_intents"][0]["static_parameters"].update(
            {"too_large": 10**16}
        ),
        lambda provider: provider["authorization_intents"][0]["static_parameters"].update(
            {"extremely_large": 10**10000}
        ),
        lambda provider: provider["authorization_intents"][0]["static_parameters"].update(
            {"not_finite": float("inf")}
        ),
    ],
)
def test_oauth_manifest_text_and_scalar_values_have_explicit_bounds(mutate) -> None:
    provider = _provider()
    mutate(provider)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False


def test_static_parameters_accept_only_supported_bounded_scalar_kinds() -> None:
    provider = _provider()
    provider["authorization_intents"][0]["static_parameters"] = {
        "string_value": "bounded",
        "true_value": True,
        "false_value": False,
        "integer_value": 42,
        "float_value": 1.5,
    }

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is True, result["errors"]


def test_provider_fingerprint_ignores_cosmetic_copy_but_changes_for_token_origin() -> None:
    original = validate_extension_contract_v2(_descriptor())
    cosmetic_provider = copy.deepcopy(_provider())
    cosmetic_provider["display_name"] = "Renamed Example Identity"
    cosmetic = validate_extension_contract_v2(_descriptor(cosmetic_provider))
    changed_provider = copy.deepcopy(_provider())
    changed_provider["token_endpoint"]["url"] = "https://tokens.example.test/token"
    changed = validate_extension_contract_v2(_descriptor(changed_provider))

    original_row = original["normalized_contract"]["oauth_providers"][0]
    cosmetic_row = cosmetic["normalized_contract"]["oauth_providers"][0]
    changed_row = changed["normalized_contract"]["oauth_providers"][0]
    assert cosmetic_row["security_fingerprint"] == original_row["security_fingerprint"]
    assert changed_row["security_fingerprint"] != original_row["security_fingerprint"]
    assert changed_row["revocation_fingerprint"] == original_row["revocation_fingerprint"]


def test_provider_fingerprints_cover_every_security_boundary_separately() -> None:
    baseline = _provider()
    baseline_compiled = compile_oauth_providers(_descriptor(baseline))[0]
    baseline_security = provider_security_fingerprint(baseline_compiled)
    baseline_revocation = provider_revocation_fingerprint(baseline_compiled)

    cosmetic_mutations = [
        {"display_name": "Cosmetic rename"},
        {"description": "Different help copy"},
    ]
    for mutation in cosmetic_mutations:
        candidate = copy.deepcopy(baseline)
        candidate.update(mutation)
        compiled = compile_oauth_providers(_descriptor(candidate))[0]
        assert provider_security_fingerprint(compiled) == baseline_security
        assert provider_revocation_fingerprint(compiled) == baseline_revocation

    security_mutations = [
        {"provider_id": "example_v2"},
        {"authorization_endpoint": {"url": "https://login.example.test/authorize"}},
        {
            "token_endpoint": {
                "url": "https://identity.example.test/token-v2",
                "client_auth_method": "client_secret_post",
            }
        },
        {
            "token_endpoint": {
                "url": "https://identity.example.test/token",
                "client_auth_method": "client_secret_basic",
            }
        },
        {"callback_id": "oauth_authorization_v2"},
        {"post_connect_action_id": "verify_identity_v2"},
        {
            "authorization_intents": [
                {
                    "intent_id": "connect",
                    "static_parameters": {"prompt": "consent"},
                    "user_inputs": [],
                }
            ]
        },
    ]
    for mutation in security_mutations:
        candidate = copy.deepcopy(baseline)
        candidate.update(mutation)
        descriptor = _descriptor(candidate)
        if candidate["provider_id"] != "example":
            for connection in descriptor["runtime"]["extension_contract_v2"]["connection_types"]:
                connection["auth"]["provider_id"] = candidate["provider_id"]
        if candidate["callback_id"] != "oauth_authorization":
            descriptor["callbacks"].append(
                {
                    "callback_id": candidate["callback_id"],
                    "purpose": "oauth_authorization",
                    "methods": ["GET"],
                    "connection_scoped": False,
                }
            )
        if candidate.get("post_connect_action_id") != "verify_identity":
            descriptor["action_definitions"].append(
                {
                    "action_id": candidate["post_connect_action_id"],
                    "handler": {"mode": "extension"},
                }
            )
        compiled = compile_oauth_providers(descriptor)[0]
        assert provider_security_fingerprint(compiled) != baseline_security, mutation
        assert provider_revocation_fingerprint(compiled) == baseline_revocation

    credential_changed = copy.deepcopy(baseline)
    credential_changed["credential_fields"][1]["change_policy"] = "reauthorization_required"
    compiled = compile_oauth_providers(_descriptor(credential_changed))[0]
    assert provider_security_fingerprint(compiled) != baseline_security

    input_validation_changed = copy.deepcopy(baseline)
    input_validation_changed["authorization_intents"][0]["user_inputs"][0]["validation"][
        "max_length"
    ] = 128
    compiled = compile_oauth_providers(_descriptor(input_validation_changed))[0]
    assert provider_security_fingerprint(compiled) != baseline_security

    cosmetic_field_added = copy.deepcopy(baseline)
    cosmetic_field_added["credential_fields"].append(
        {
            "field_id": "configuration_note",
            "value_type": "text",
            "required": False,
            "secret": False,
            "change_policy": "cosmetic",
            "label": "Configuration note",
            "help_text": "Optional extension-owned note.",
            "autocomplete": "off",
            "validation": {"min_length": 0, "max_length": 500},
        }
    )
    compiled = compile_oauth_providers(_descriptor(cosmetic_field_added))[0]
    assert provider_security_fingerprint(compiled) == baseline_security

    revocation_changed = copy.deepcopy(baseline)
    revocation_changed["revocation_endpoint"]["url"] = "https://identity.example.test/revoke-v2"
    compiled = compile_oauth_providers(_descriptor(revocation_changed))[0]
    assert provider_security_fingerprint(compiled) == baseline_security
    assert provider_revocation_fingerprint(compiled) != baseline_revocation


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda provider: provider["credential_fields"].append(
                copy.deepcopy(provider["credential_fields"][0])
            ),
            "duplicate_oauth_credential_field",
        ),
        (
            lambda provider: provider["authorization_intents"].append(
                copy.deepcopy(provider["authorization_intents"][0])
            ),
            "duplicate_oauth_intent",
        ),
        (
            lambda provider: provider["authorization_intents"][0]["user_inputs"].append(
                copy.deepcopy(provider["authorization_intents"][0]["user_inputs"][0])
            ),
            "duplicate_oauth_intent_input",
        ),
        (
            lambda provider: provider["authorization_intents"][0]["user_inputs"].append(
                {
                    "input_id": "other_hint",
                    "parameter": "login_hint",
                    "validation": {"type": "text", "max_length": 50},
                }
            ),
            "duplicate_oauth_intent_parameter",
        ),
    ],
)
def test_nested_oauth_ids_and_parameter_mappings_are_unique(mutate, expected_code: str) -> None:
    provider = copy.deepcopy(_provider())
    mutate(provider)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"provider_id": "Not Stable"}, "invalid_oauth_identifier"),
        ({"display_name": ""}, "missing_oauth_display_name"),
        ({"description": ""}, "missing_oauth_description"),
        (
            {
                "credential_fields": [
                    {
                        "field_id": "client_id",
                        "value_type": "text",
                        "required": "yes",
                        "secret": False,
                        "change_policy": "reauthorization_required",
                    }
                ]
            },
            "invalid_oauth_credential_field",
        ),
        (
            {
                "credential_fields": [
                    {
                        "field_id": "client_secret",
                        "value_type": "secret",
                        "required": True,
                        "secret": "yes",
                        "change_policy": "runtime_revalidate",
                    }
                ]
            },
            "invalid_oauth_credential_field",
        ),
        (
            {
                "credential_fields": [
                    {
                        "field_id": "client_secret",
                        "value_type": "secret",
                        "required": True,
                        "secret": False,
                        "change_policy": "runtime_revalidate",
                    }
                ]
            },
            "invalid_oauth_credential_field",
        ),
        (
            {
                "credential_fields": [
                    {
                        "field_id": "client_id",
                        "value_type": "text",
                        "required": True,
                        "secret": False,
                        "change_policy": "reauthorization_required",
                    }
                ]
            },
            "missing_oauth_client_secret",
        ),
    ],
)
def test_provider_identity_and_credential_schema_are_closed(
    mutation: dict, expected_code: str
) -> None:
    provider = copy.deepcopy(_provider())
    provider.update(mutation)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("credential", "Invalid Field"),
        ("intent", "Invalid Intent"),
        ("input", "Invalid Input"),
        ("callback", "Invalid Callback"),
        ("action", "Invalid Action"),
        ("credential", "a" * 65),
        ("intent", "a" * 65),
        ("input", "a" * 65),
        ("callback", "a" * 65),
        ("action", "a" * 65),
    ],
)
def test_every_oauth_reference_namespace_uses_stable_slug_ids(target: str, value: str) -> None:
    provider = copy.deepcopy(_provider())
    if target == "credential":
        provider["credential_fields"][0]["field_id"] = value
    elif target == "intent":
        provider["authorization_intents"][0]["intent_id"] = value
    elif target == "input":
        provider["authorization_intents"][0]["user_inputs"][0]["input_id"] = value
    elif target == "callback":
        provider["callback_id"] = value
    else:
        provider["post_connect_action_id"] = value

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "invalid_oauth_identifier" in _error_codes(result)


def test_public_client_auth_rejects_declared_secret_and_static_values_are_bounded() -> None:
    public_provider = copy.deepcopy(_provider())
    public_provider["token_endpoint"]["client_auth_method"] = "none"
    public_result = validate_extension_contract_v2(_descriptor(public_provider))

    static_provider = copy.deepcopy(_provider())
    static_provider["authorization_intents"][0]["static_parameters"] = {
        "provider_options": {"unbounded": True}
    }
    static_result = validate_extension_contract_v2(_descriptor(static_provider))

    assert "incompatible_oauth_client_auth_method" in _error_codes(public_result)
    assert "invalid_oauth_intent_parameter" in _error_codes(static_result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"rename": ("client_id", "application_id")}, "missing_oauth_client_id"),
        ({"rename": ("client_secret", "application_secret")}, "missing_oauth_client_secret"),
        ({"duplicate": "client_id"}, "duplicate_oauth_credential_field"),
    ],
)
def test_token_transport_uses_exact_credential_roles(mutation: dict, expected_code: str) -> None:
    provider = copy.deepcopy(_provider())
    if "rename" in mutation:
        source, replacement = mutation["rename"]
        next(field for field in provider["credential_fields"] if field["field_id"] == source)[
            "field_id"
        ] = replacement
    else:
        source = mutation["duplicate"]
        provider["credential_fields"].append(
            copy.deepcopy(
                next(
                    field for field in provider["credential_fields"] if field["field_id"] == source
                )
            )
        )

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


def test_public_client_requires_client_id_and_forbids_client_secret() -> None:
    provider = copy.deepcopy(_provider())
    provider["token_endpoint"]["client_auth_method"] = "none"
    provider["credential_fields"] = [
        field for field in provider["credential_fields"] if field["field_id"] == "client_id"
    ]

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is True, result["errors"]


@pytest.mark.parametrize(
    ("target", "unknown_key"),
    [
        ("provider", "dispaly_name"),
        ("flow", "pkce_mode"),
        ("credential", "requred"),
        ("authorization_endpoint", "uri"),
        ("token_endpoint", "auth_method"),
        ("intent", "parameters"),
        ("input", "param"),
        ("validation", "maxlength"),
    ],
)
def test_oauth_manifest_objects_reject_unknown_keys(target: str, unknown_key: str) -> None:
    provider = copy.deepcopy(_provider())
    if target == "provider":
        provider[unknown_key] = "typo"
    elif target == "flow":
        provider["flow"][unknown_key] = "typo"
    elif target == "credential":
        provider["credential_fields"][0][unknown_key] = "typo"
    elif target in {"authorization_endpoint", "token_endpoint"}:
        provider[target][unknown_key] = "typo"
    elif target == "intent":
        provider["authorization_intents"][0][unknown_key] = "typo"
    elif target == "input":
        provider["authorization_intents"][0]["user_inputs"][0][unknown_key] = "typo"
    else:
        provider["authorization_intents"][0]["user_inputs"][0]["validation"][unknown_key] = "typo"

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "unknown_oauth_contract_key" in _error_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"provider_id": 123}, "invalid_oauth_identifier"),
        ({"display_name": 123}, "missing_oauth_display_name"),
        ({"description": 123}, "missing_oauth_description"),
        (
            {"revocation_endpoint": "https://identity.example.test/revoke"},
            "invalid_oauth_endpoint",
        ),
    ],
)
def test_oauth_contract_does_not_coerce_malformed_scalar_or_object_types(
    mutation: dict, expected_code: str
) -> None:
    provider = copy.deepcopy(_provider())
    provider.update(mutation)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


def test_non_object_provider_row_is_rejected_instead_of_dropped() -> None:
    descriptor = _descriptor()
    descriptor["runtime"]["extension_contract_v2"]["oauth_providers"].append("not-an-object")

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "invalid_oauth_provider" in _error_codes(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"flow": {"type": "device_code", "pkce": "s256"}}, "invalid_oauth_flow"),
        (
            {"token_endpoint": {"url": "http://identity.example.test/token"}},
            "unsafe_oauth_endpoint",
        ),
        (
            {
                "token_endpoint": {
                    "url": "https://identity.example.test/token",
                    "client_auth_method": "private_key_jwt",
                }
            },
            "unsupported_oauth_client_auth_method",
        ),
        ({"callback_id": "missing_callback"}, "unknown_oauth_callback"),
        ({"post_connect_action_id": "missing_action"}, "unknown_oauth_post_connect_action"),
    ],
)
def test_invalid_provider_contract_is_rejected(mutation: dict, expected_code: str) -> None:
    provider = copy.deepcopy(_provider())
    provider.update(mutation)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


def test_direct_compiler_does_not_defer_missing_post_connect_action() -> None:
    descriptor = _descriptor()
    descriptor["action_definitions"] = []

    with pytest.raises(ValueError, match="unknown_oauth_post_connect_action"):
        compile_oauth_providers(descriptor)


@pytest.mark.parametrize(
    "mutation",
    [
        {"handler": {"mode": "platform"}},
        {"mutates_platform": True, "command_type": "invoke_extension_action"},
        {"workflow_visible": True},
        {"parameters": [{"name": "unsafe", "type": "string", "required": False}]},
        {"external_effect": True},
    ],
)
def test_direct_contract_rejects_unsafe_automatic_post_connect_action(mutation: dict) -> None:
    descriptor = _descriptor()
    action = descriptor["action_definitions"][0]
    external_effect = mutation.pop("external_effect", False)
    action.update(mutation)
    if external_effect:
        descriptor["external_effects"] = [
            {
                "operation_id": "verify_identity",
                "effect_kind": "identity_mutation",
                "channel": "identity",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
            }
        ]

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "unsafe_oauth_post_connect_action" in _error_codes(result)
    with pytest.raises(ValueError, match="unsafe_oauth_post_connect_action"):
        compile_oauth_providers(descriptor)


@pytest.mark.parametrize(
    "mutation",
    [
        {"handler": {"mode": "platform"}},
        {"mutates_platform": True, "command_type": "invoke_extension_action"},
        {"workflow_visible": True},
        {"parameters": [{"name": "unsafe", "type": "string", "required": False}]},
        {"external_effect": True},
    ],
)
def test_compiled_bundle_rejects_unsafe_automatic_post_connect_action(
    tmp_path: Path, mutation: dict
) -> None:
    source = Path(__file__).parents[1] / "fixtures" / "extensions" / "fixture_extension_oauth"
    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(source, bundle)
    action_path = bundle / "ui" / "actions" / "actions.yaml"
    actions = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    external_effect = mutation.pop("external_effect", False)
    actions["actions"][0].update(mutation)
    action_path.write_text(yaml.safe_dump(actions, sort_keys=False), encoding="utf-8")
    if external_effect:
        manifest_path = bundle / "extension.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["external_effects"] = [
            {
                "operation_id": "verify_identity",
                "effect_kind": "identity_mutation",
                "channel": "identity",
                "idempotency": "required",
                "test_mode_behavior": "suppress",
            }
        ]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = compile_extension_bundle(bundle)

    assert result["status"] == "invalid"
    assert "unsafe_oauth_post_connect_action" in {
        str(item.get("code") or "")
        for item in result["validation_errors"] + result["compile_errors"]
    }


def test_connection_type_provider_reference_must_resolve_inside_extension() -> None:
    descriptor = _descriptor()
    descriptor["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"]["provider_id"] = (
        "foreign"
    )

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "unknown_connection_oauth_provider" in _error_codes(result)


def test_provider_ids_are_unique_only_inside_each_extension_manifest() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "extensions"
    results = []
    for fixture_name in ("fixture_extension_oauth", "fixture_extension_oauth_peer"):
        descriptor = yaml.safe_load(
            (fixture_root / fixture_name / "extension.yaml").read_text(encoding="utf-8")
        )
        results.append(validate_extension_contract_v2(descriptor))

    assert all(result["valid"] for result in results), results
    assert [
        result["normalized_contract"]["oauth_providers"][0]["provider_id"] for result in results
    ] == ["example", "example"]

    duplicate = _descriptor()
    duplicate["runtime"]["extension_contract_v2"]["oauth_providers"].append(
        copy.deepcopy(_provider())
    )
    rejected = validate_extension_contract_v2(duplicate)
    assert rejected["valid"] is False
    assert "duplicate_oauth_provider" in _error_codes(rejected)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            {"token_endpoint": {"url": "https://127.0.0.1/token", "client_auth_method": "none"}},
            "unsafe_oauth_endpoint",
        ),
        (
            {
                "authorization_intents": [
                    {
                        "intent_id": "connect",
                        "static_parameters": {},
                        "user_inputs": [{"input_id": "hint"}],
                    }
                ]
            },
            "invalid_oauth_intent_input",
        ),
    ],
)
def test_provider_security_permissions_are_closed(mutation: dict, expected_code: str) -> None:
    provider = copy.deepcopy(_provider())
    provider.update(mutation)

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://identity.example.test/token?audience=mutable",
        "https://identity.example.test/token?",
        "https://identity.example.test/token#fragment",
        "https://identity.example.test/token#",
        "https://identity.example.test/{tenant}/token",
        "https://identity.example.test:invalid/token",
        "https://identity.example.test:0/token",
        "https://user:password@identity.example.test/token",
        "https://[::1/token",
        "https://[not-an-ip]/token",
    ],
)
def test_v1_endpoints_are_immutable_absolute_urls(url: str) -> None:
    provider = copy.deepcopy(_provider())
    provider["token_endpoint"]["url"] = url

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "unsafe_oauth_endpoint" in _error_codes(result)


@pytest.mark.parametrize(
    ("target", "reserved_parameter"),
    [("static", "state"), ("input", "redirect_uri"), ("input", "code_challenge")],
)
def test_protocol_owned_authorization_parameters_are_reserved(
    target: str, reserved_parameter: str
) -> None:
    provider = copy.deepcopy(_provider())
    intent = provider["authorization_intents"][0]
    if target == "static":
        intent["static_parameters"] = {reserved_parameter: "attacker-value"}
    else:
        intent["user_inputs"][0]["parameter"] = reserved_parameter

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "reserved_oauth_parameter" in _error_codes(result)


def test_static_and_user_input_parameters_cannot_conflict() -> None:
    provider = copy.deepcopy(_provider())
    provider["authorization_intents"][0]["static_parameters"] = {"login_hint": "fixed@example.test"}

    result = validate_extension_contract_v2(_descriptor(provider))

    assert result["valid"] is False
    assert "duplicate_oauth_intent_parameter" in _error_codes(result)


def test_runtime_intent_inputs_are_declared_validated_and_mapped() -> None:
    provider = compile_oauth_providers(_descriptor())[0]

    assert validate_authorization_intent_inputs(
        provider,
        intent_id="connect",
        values={"email_hint": "  person@example.test  "},
    ) == {"login_hint": "person@example.test"}

    with pytest.raises(ValueError, match="unknown_oauth_intent_input"):
        validate_authorization_intent_inputs(
            provider,
            intent_id="connect",
            values={"return_url": "https://attacker.example"},
        )
    with pytest.raises(ValueError, match="invalid_oauth_intent_input_value"):
        validate_authorization_intent_inputs(
            provider,
            intent_id="connect",
            values={"email_hint": "not-an-email"},
        )


@pytest.mark.parametrize(
    ("scopes", "expected_code"),
    [
        ({"required": "mail.read", "optional": []}, "invalid_connection_auth_oauth_scopes"),
        ({"required": [], "optional": []}, "missing_connection_auth_oauth_required_scope"),
        (
            {"required": ["mail.read", "mail.read"], "optional": []},
            "duplicate_connection_auth_oauth_scope",
        ),
        (
            {"required": ["mail.read"], "optional": ["mail.read"]},
            "overlapping_connection_auth_oauth_scope",
        ),
    ],
)
def test_provider_reference_uses_strict_scope_validation(
    scopes: object, expected_code: str
) -> None:
    descriptor = _descriptor()
    descriptor["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"]["scopes"] = scopes

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert expected_code in _error_codes(result)


def test_optional_post_connect_action_is_absent_in_normalized_contract() -> None:
    provider = copy.deepcopy(_provider())
    provider.pop("post_connect_action_id")

    compiled = compile_oauth_providers(_descriptor(provider))[0]
    result = validate_extension_contract_v2(_descriptor(provider))

    assert compiled.post_connect_action_id is None
    assert "post_connect_action_id" not in result["normalized_contract"]["oauth_providers"][0]


def test_reserved_oauth_callback_is_core_owned_and_requires_no_action() -> None:
    manifest = _compiled_manifest(
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    )
    result = validate_compiled_manifest(manifest)

    assert result["status"] == "valid", result["errors"]
    assert manifest["callbacks"] == [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
            "auth": {"type": "none"},
        }
    ]


@pytest.mark.parametrize(
    ("extra", "expected_code"),
    [
        ({"action_id": "ingress_event"}, "oauth_callback_action_forbidden"),
        ({"connection_scoped": True}, "oauth_callback_must_be_project_scoped"),
        (
            {"auth": {"type": "shared_secret_header", "header": "X-Secret"}},
            "oauth_callback_auth_forbidden",
        ),
    ],
)
def test_reserved_oauth_callback_rejects_extension_dispatch_fields(
    extra: dict, expected_code: str
) -> None:
    callback = {
        "callback_id": "oauth_authorization",
        "purpose": "oauth_authorization",
        "methods": ["GET"],
        "connection_scoped": False,
        **extra,
    }

    result = validate_compiled_manifest(_compiled_manifest(callback))

    assert result["status"] == "invalid"
    assert expected_code in _error_codes(result)


def test_reserved_oauth_callback_requires_explicit_project_scope() -> None:
    result = validate_compiled_manifest(
        _compiled_manifest(
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": ["GET"],
            }
        )
    )

    assert result["status"] == "invalid"
    assert "oauth_callback_must_be_project_scoped" in _error_codes(result)


@pytest.mark.parametrize("methods", [["POST"], ["GET", "POST"]])
def test_reserved_oauth_callback_requires_exact_get_method(methods: list[str]) -> None:
    result = validate_compiled_manifest(
        _compiled_manifest(
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": methods,
                "connection_scoped": False,
            }
        )
    )

    assert result["status"] == "invalid"
    assert "unsupported_oauth_callback_method" in _error_codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        {"methods": ["POST"]},
        {"connection_scoped": True},
        {"action_id": "verify_identity"},
        {"auth": {"type": "shared_secret_header", "header": "X-Secret"}},
    ],
)
def test_provider_reference_resolves_only_a_valid_reserved_callback(
    mutation: dict,
) -> None:
    descriptor = _descriptor()
    descriptor["callbacks"][0].update(mutation)

    result = validate_extension_contract_v2(descriptor)

    assert result["valid"] is False
    assert "unknown_oauth_callback" in _error_codes(result)


def test_inbound_callback_keeps_existing_action_and_auth_contract() -> None:
    result = validate_compiled_manifest(
        _compiled_manifest(
            {
                "callback_id": "updates",
                "purpose": "inbound_events",
                "methods": ["POST"],
                "action_id": "ingress_event",
                "connection_scoped": True,
                "auth": {
                    "type": "shared_secret_header",
                    "header": "X-Fixture-Callback-Secret",
                },
            }
        )
    )

    assert result["status"] == "valid", result["errors"]
