from __future__ import annotations

import copy
from typing import Any

_BROWSER_MANIFEST_FIELDS = frozenset(
    {
        "account_scope",
        "author",
        "billing_mode",
        "compile_status",
        "connection_types",
        "description",
        "display_name",
        "extension_id",
        "features",
        "issues_url",
        "kind",
        "listing",
        "locale_bundles",
        "locale_codes",
        "manifest_version",
        "oauth_providers",
        "project_state",
        "release_stage",
        "schemas",
        "screenshots",
        "step_ui_manifest",
        "support_url",
        "supported_url_schemes",
        "ui_manifest",
        "upgrade_notes",
        "validation_status",
        "vendor",
        "vendor_name",
        "version",
        "website_url",
    }
)


def _public_operations(raw_operations: Any) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for raw_operation in raw_operations or []:
        if not isinstance(raw_operation, dict):
            continue
        operation = {
            key: copy.deepcopy(raw_operation[key])
            for key in ("operation_id", "inputs", "public_inputs")
            if key in raw_operation
        }
        if operation:
            operations.append(operation)
    return operations


def _public_oauth_providers(raw_providers: Any) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for raw_provider in raw_providers or []:
        if not isinstance(raw_provider, dict):
            continue
        provider = {
            key: copy.deepcopy(raw_provider[key])
            for key in (
                "provider_id",
                "display_name",
                "description",
                "setup_page_id",
                "help_page_id",
                "error_help_anchors",
                "authorization_endpoint",
                "token_endpoint",
                "revocation_endpoint",
            )
            if key in raw_provider
        }
        credential_fields: list[dict[str, Any]] = []
        for raw_field in raw_provider.get("credential_fields") or []:
            if not isinstance(raw_field, dict):
                continue
            field = {
                key: copy.deepcopy(raw_field[key])
                for key in (
                    "field_id",
                    "value_type",
                    "required",
                    "secret",
                    "label",
                    "help_text",
                    "placeholder",
                    "autocomplete",
                    "validation",
                )
                if key in raw_field
            }
            if field:
                credential_fields.append(field)
        provider["credential_fields"] = credential_fields
        intents: list[dict[str, Any]] = []
        for raw_intent in raw_provider.get("authorization_intents") or []:
            if not isinstance(raw_intent, dict):
                continue
            intent = {
                key: copy.deepcopy(raw_intent[key]) for key in ("intent_id",) if key in raw_intent
            }
            user_inputs: list[dict[str, Any]] = []
            for raw_input in raw_intent.get("user_inputs") or []:
                if not isinstance(raw_input, dict):
                    continue
                declared_input = {
                    key: copy.deepcopy(raw_input[key])
                    for key in ("input_id", "validation")
                    if key in raw_input
                }
                if declared_input:
                    user_inputs.append(declared_input)
            intent["user_inputs"] = user_inputs
            if intent:
                intents.append(intent)
        provider["authorization_intents"] = intents
        if provider:
            providers.append(provider)
    return providers


def project_browser_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project the closed manifest surface consumed by authenticated browser UI."""
    projected = {
        key: copy.deepcopy(manifest[key]) for key in _BROWSER_MANIFEST_FIELDS if key in manifest
    }
    if "oauth_providers" in manifest:
        projected["oauth_providers"] = _public_oauth_providers(manifest["oauth_providers"])
    action_rows: list[dict[str, Any]] = []
    for raw_row in manifest.get("action_definitions") or []:
        if not isinstance(raw_row, dict):
            continue
        row = {
            key: copy.deepcopy(raw_row[key])
            for key in ("action_id", "id", "label", "description", "parameters")
            if key in raw_row
        }
        if row:
            action_rows.append(row)
    if action_rows:
        projected["action_definitions"] = action_rows
    top_level_operations = _public_operations(manifest.get("operation_manifest"))
    if top_level_operations:
        projected["operation_manifest"] = top_level_operations
    normalized_contract = manifest.get("extension_contract_v2_normalized")
    if isinstance(normalized_contract, dict):
        operations = _public_operations(normalized_contract.get("operation_manifest"))
        if operations:
            projected["extension_contract_v2_normalized"] = {"operation_manifest": operations}
    runtime = manifest.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    contract = runtime.get("extension_contract_v2")
    contract = contract if isinstance(contract, dict) else {}
    llm_provider = contract.get("llm_provider")
    if isinstance(llm_provider, dict):
        projected["runtime"] = {
            "extension_contract_v2": {"llm_provider": copy.deepcopy(llm_provider)}
        }
    return projected


__all__ = ["project_browser_manifest"]
