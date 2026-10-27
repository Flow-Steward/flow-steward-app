"""Extension Contract v2 loader, validator, and deterministic preview helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.application.extensions.extension_contract_loading import (
    _extract_contract_root,
    _load_section_payload,
    _normalize_section,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_ARTIFACT_POLICY_KINDS as ALLOWED_ARTIFACT_POLICY_KINDS,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_ARTIFACT_POLICY_RETENTION_CLASSES as ALLOWED_ARTIFACT_POLICY_RETENTION_CLASSES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_ARTIFACT_POLICY_VALUE_TYPES as ALLOWED_ARTIFACT_POLICY_VALUE_TYPES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_CONNECTION_AUTH_TYPES as ALLOWED_CONNECTION_AUTH_TYPES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_CURSOR_MODES as ALLOWED_CURSOR_MODES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_DATASET_RUNTIME_KINDS as ALLOWED_DATASET_RUNTIME_KINDS,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_OPERATION_KINDS as ALLOWED_OPERATION_KINDS,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_PARTIAL_FAILURE_MODES as ALLOWED_PARTIAL_FAILURE_MODES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_VALUE_TYPES as ALLOWED_VALUE_TYPES,
)
from core.application.extensions.extension_contract_policies import (
    ALLOWED_WIDGET_TYPES as ALLOWED_WIDGET_TYPES,
)
from core.application.extensions.extension_contract_policies import (
    ARTIFACT_POLICY_GRANT_GROUPS as ARTIFACT_POLICY_GRANT_GROUPS,
)
from core.application.extensions.extension_contract_policies import (
    RESERVED_ARTIFACT_POLICY_BINDING_KEYS as RESERVED_ARTIFACT_POLICY_BINDING_KEYS,
)
from core.application.extensions.extension_contract_policies import (
    VALUE_TYPE_JSON_SCHEMA_TYPES as VALUE_TYPE_JSON_SCHEMA_TYPES,
)
from core.application.extensions.extension_contract_references import (
    _validate_ids_and_references,
)
from core.application.extensions.extension_contract_v2_common import (
    _as_bool,
    _as_text,
    _error,
    _normalize_lower,
    _positive_int,
)
from core.application.extensions.extension_locales import (
    load_locale_bundles,
    ui_manifest_locale_keys,
)
from core.application.extensions.oauth_contract import (
    declared_oauth_callback_ids,
    is_safe_oauth_post_connect_action,
    normalize_oauth_providers,
    oauth_locale_copy_limits,
)

ROOT_SECTIONS: tuple[str, ...] = (
    "oauth_providers",
    "connection_types",
    "dataset_connectors",
    "operation_manifest",
    "step_ui_manifest",
    "resource_query_manifest",
    "event_bindings",
    "artifact_policies",
)


def validate_extension_contract_v2(
    descriptor: dict[str, Any],
    *,
    bundle_root: str | Path | None = None,
    descriptor_url: str = "",
    url_fetcher: Callable[[str, float], str] | None = None,
    timeout_seconds: float = 15.0,
    ui_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load, validate, and preview Extension Contract v2 for a descriptor.

    The extension's ``locales/`` dictionaries are loaded whether or not it
    declares a contract: an extension with pages and no operations still has
    text to translate. ``ui_manifest`` is the resolved UI manifest, when the
    caller has it, so the keys its pages use are not reported as orphans.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    contract_root = _extract_contract_root(descriptor)
    if contract_root is None:
        locale_bundles = _load_locale_bundles(
            bundle_root=Path(bundle_root).resolve() if bundle_root else None,
            warnings=warnings,
            errors=errors,
        )
        _check_locale_keys(
            locale_bundles,
            expected_keys=derive_expected_locale_keys({}, descriptor=descriptor)
            | ui_manifest_locale_keys(ui_manifest),
            oauth_locale_limits={},
            errors=errors,
            warnings=warnings,
        )
        return {
            "present": False,
            "valid": not errors,
            "normalized_contract": {},
            "locale_bundles": locale_bundles,
            "locale_codes": sorted(locale_bundles),
            "errors": errors,
            "warnings": warnings,
            "preview": {
                "sections_loaded": [],
                "counts": {},
                "resolved_ids": {},
                "builder_operations": [],
                "errors": [],
                "warnings": [],
            },
        }

    root_path = Path(bundle_root).resolve() if bundle_root else None
    normalized_contract: dict[str, list[dict[str, Any]]] = {}
    for section in ROOT_SECTIONS:
        loaded = _load_section_payload(
            section,
            contract_root.get(section),
            contract_root=contract_root,
            bundle_root=root_path,
            descriptor_url=descriptor_url,
            url_fetcher=url_fetcher,
            timeout_seconds=timeout_seconds,
            errors=errors,
        )
        if section == "oauth_providers":
            provider_rows: Any = loaded
            if isinstance(loaded, dict):
                for key in ("oauth_providers", "providers", "items"):
                    if isinstance(loaded.get(key), list):
                        provider_rows = loaded[key]
                        break
            if isinstance(provider_rows, list):
                for provider_idx, provider_row in enumerate(provider_rows):
                    if not isinstance(provider_row, dict):
                        _error(
                            errors,
                            path=f"extension_contract_v2.oauth_providers[{provider_idx}]",
                            message="OAuth provider declaration must be an object",
                            code="invalid_oauth_provider",
                        )
        normalized_contract[section] = _normalize_section(section, loaded)

    callback_ids = declared_oauth_callback_ids(descriptor)
    action_ids = {
        _as_text(row.get("action_id"))
        for row in descriptor.get("action_definitions", [])
        if isinstance(row, dict) and _as_text(row.get("action_id"))
    }
    normalized_contract["oauth_providers"] = normalize_oauth_providers(
        normalized_contract["oauth_providers"],
        callback_ids=callback_ids,
        action_ids=action_ids,
        # Bundle action manifests resolve after Contract v2. Inline declarations
        # can be checked here; compiled-manifest validation checks every ref.
        validate_action_references=bool(action_ids),
        errors=errors,
    )
    if action_ids:
        for provider_idx, provider in enumerate(normalized_contract["oauth_providers"]):
            action_id = _as_text(provider.get("post_connect_action_id"))
            if action_id and not is_safe_oauth_post_connect_action(descriptor, action_id):
                _error(
                    errors,
                    path=(
                        f"extension_contract_v2.oauth_providers[{provider_idx}]"
                        ".post_connect_action_id"
                    ),
                    message="Post-connect actions must use the closed automatic action profile",
                    code="unsafe_oauth_post_connect_action",
                )

    _validate_ids_and_references(
        normalized_contract,
        errors=errors,
    )

    locale_bundles = _load_locale_bundles(
        bundle_root=root_path,
        warnings=warnings,
        errors=errors,
    )
    _check_locale_keys(
        locale_bundles,
        expected_keys=derive_expected_locale_keys(normalized_contract, descriptor=descriptor)
        | ui_manifest_locale_keys(ui_manifest),
        oauth_locale_limits=oauth_locale_copy_limits(normalized_contract["oauth_providers"]),
        errors=errors,
        warnings=warnings,
    )

    preview = _build_preview(
        normalized_contract,
        errors=errors,
        warnings=warnings,
    )
    return {
        "present": True,
        "valid": not errors,
        "normalized_contract": normalized_contract,
        "locale_bundles": locale_bundles,
        "locale_codes": sorted(locale_bundles.keys()) if locale_bundles else [],
        "errors": errors,
        "warnings": warnings,
        "preview": preview,
    }


def _build_preview(
    contract: dict[str, list[dict[str, Any]]],
    *,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    counts = {section: len(rows) for section, rows in contract.items()}
    sections_loaded = [section for section, count in counts.items() if count > 0]

    operation_rows = contract.get("operation_manifest", [])
    derived_builder_operations = [
        {
            "operation_id": _as_text(row.get("operation_id")),
            "display_name": _as_text(
                row.get("display_name") or row.get("name") or row.get("operation_id")
            ),
            "operation_kind": _normalize_lower(
                row.get("operation_kind") or row.get("kind") or "action"
            ),
            "supports_batch": _as_bool(row.get("supports_batch")),
            "preferred_batch_size": _positive_int(row.get("preferred_batch_size")),
            "max_batch_size": _positive_int(row.get("max_batch_size")),
            "batch_input_field": _as_text(row.get("batch_input_field")),
            "partial_failure_mode": _normalize_lower(row.get("partial_failure_mode")),
            "supports_async_job": (
                row.get("supports_async_job")
                if isinstance(row.get("supports_async_job"), bool)
                else None
            ),
        }
        for row in operation_rows
        if _as_text(row.get("operation_id"))
    ]
    derived_builder_operations.sort(key=lambda row: str(row.get("operation_id") or ""))

    resolved_ids = {
        "oauth_provider_ids": sorted(
            _as_text(row.get("provider_id"))
            for row in contract.get("oauth_providers", [])
            if _as_text(row.get("provider_id"))
        ),
        "connection_type_ids": sorted(
            _as_text(row.get("connection_type_id"))
            for row in contract.get("connection_types", [])
            if _as_text(row.get("connection_type_id"))
        ),
        "dataset_connector_ids": sorted(
            _as_text(row.get("connector_id"))
            for row in contract.get("dataset_connectors", [])
            if _as_text(row.get("connector_id"))
        ),
        "operation_ids": sorted(
            _as_text(row.get("operation_id"))
            for row in contract.get("operation_manifest", [])
            if _as_text(row.get("operation_id"))
        ),
        "query_ids": sorted(
            _as_text(row.get("query_id"))
            for row in contract.get("resource_query_manifest", [])
            if _as_text(row.get("query_id"))
        ),
        "trigger_ids": sorted(
            _as_text(row.get("trigger_id"))
            for row in contract.get("event_bindings", [])
            if _as_text(row.get("trigger_id"))
        ),
    }
    sorted_errors = sorted(
        errors, key=lambda row: (row.get("path", ""), row.get("code", ""), row.get("message", ""))
    )
    sorted_warnings = sorted(
        warnings, key=lambda row: (row.get("path", ""), row.get("code", ""), row.get("message", ""))
    )
    return {
        "sections_loaded": sections_loaded,
        "counts": counts,
        "resolved_ids": resolved_ids,
        "builder_operations": derived_builder_operations,
        "errors": sorted_errors,
        "warnings": sorted_warnings,
    }


def _load_locale_bundles(
    *,
    bundle_root: Path | None,
    warnings: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Load the extension's ``locales/*.yaml`` dictionaries, reporting bad files."""
    bundles, locale_errors, locale_warnings = load_locale_bundles(bundle_root)
    errors.extend(locale_errors)
    warnings.extend(locale_warnings)
    return bundles


def _check_locale_keys(
    locale_bundles: dict[str, dict[str, str]],
    *,
    expected_keys: set[str],
    oauth_locale_limits: dict[str, int],
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    """OAuth copy length limits, and keys no declaration uses."""
    if not locale_bundles:
        return
    for locale_code, bundle in locale_bundles.items():
        for key, maximum in oauth_locale_limits.items():
            value = bundle.get(key)
            if value is not None and len(value) > maximum:
                _error(
                    errors,
                    path=f"locales/{locale_code}.yaml.{key}",
                    message=f"Localized OAuth copy exceeds {maximum} characters",
                    code="invalid_oauth_locale_value",
                )
    en_keys = set(locale_bundles.get("en", {}).keys())
    for key in sorted(en_keys - expected_keys):
        warnings.append(
            {
                "path": "locales/en.yaml",
                "message": f"Locale key '{key}' does not match any manifest field",
                "code": "locale_orphan_key",
            }
        )
    for loc, bundle in locale_bundles.items():
        if loc == "en":
            continue
        for key in sorted(set(bundle.keys()) - en_keys):
            warnings.append(
                {
                    "path": f"locales/{loc}.yaml",
                    "message": f"Key '{key}' not present in en.yaml (dead key)",
                    "code": "locale_dead_key",
                }
            )


def derive_expected_locale_keys(
    contract: dict[str, list[dict[str, Any]]],
    *,
    descriptor: dict[str, Any] | None = None,
) -> set[str]:
    """Return the set of locale keys expected from the contract structure."""
    keys: set[str] = set()
    keys.update(["ext.display_name", "ext.description"])
    if descriptor and (descriptor.get("account_scope") or {}).get("description"):
        keys.add("ext.account_scope_description")
    for ct in contract.get("connection_types", []):
        cid = _as_text(ct.get("connection_type_id"))
        if cid:
            keys.update([f"connection.{cid}.display_name", f"connection.{cid}.description"])
    keys.update(oauth_locale_copy_limits(contract.get("oauth_providers", [])))
    for connector in contract.get("dataset_connectors", []):
        connector_id = _as_text(connector.get("connector_id"))
        if connector_id:
            keys.update(
                [
                    f"dataset_connector.{connector_id}.display_name",
                    f"dataset_connector.{connector_id}.description",
                ]
            )
    for op in contract.get("operation_manifest", []):
        oid = _as_text(op.get("operation_id"))
        if not oid:
            continue
        keys.update([f"op.{oid}.display_name", f"op.{oid}.description"])
        for inp in op.get("public_inputs") or op.get("inputs") or []:
            name = _as_text(inp.get("name"))
            if name:
                keys.update(
                    [f"op.{oid}.input.{name}.display_name", f"op.{oid}.input.{name}.description"]
                )
        for out in op.get("public_outputs") or op.get("outputs") or []:
            name = _as_text(out.get("name"))
            if name:
                keys.update(
                    [f"op.{oid}.output.{name}.display_name", f"op.{oid}.output.{name}.description"]
                )
        category = _as_text(op.get("category"))
        if category:
            keys.add(f"category.{category}.display_name")
    for step in contract.get("step_ui_manifest", []):
        oid = _as_text(step.get("operation_id"))
        if not oid:
            continue
        keys.add(f"step.{oid}.section")
        for field in step.get("fields") or []:
            fname = _as_text(field.get("name") if isinstance(field, dict) else "")
            if fname:
                keys.update(
                    [
                        f"step.{oid}.field.{fname}.label",
                        f"step.{oid}.field.{fname}.help_text",
                        f"step.{oid}.field.{fname}.placeholder",
                    ]
                )
    for q in contract.get("resource_query_manifest", []):
        qid = _as_text(q.get("query_id"))
        if qid:
            keys.add(f"query.{qid}.display_name")
    for eb in contract.get("event_bindings", []):
        tid = _as_text(eb.get("trigger_id"))
        if tid:
            keys.add(f"trigger.{tid}.display_name")
    return keys
