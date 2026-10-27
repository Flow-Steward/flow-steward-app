"""Release-bound workflow surfaces for one installed extension."""

from __future__ import annotations

from typing import Any

from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.application.extensions.extension_locales import resolve_locale_text

_PUBLIC_OAUTH_LIFECYCLE_STATUSES = {
    "connected",
    "disconnected",
    "pending",
    "needs_attention",
    "authorization_required",
}


def _declared_operation_connection_types(
    operation_contract: Any,
) -> list[str]:
    """Connection types one Contract V2 operation declares, in stable order."""
    ordered: list[str] = []
    row = operation_contract if isinstance(operation_contract, dict) else {}
    plural = row.get("connection_type_ids")
    values = plural if isinstance(plural, (list, tuple, set, frozenset)) else ()
    for value in values:
        text = str(value or "").strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def _localized_io(
    rows: list[dict[str, Any]],
    *,
    prefix: str,
    locale: str,
    locale_bundles: dict[str, dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Inputs or outputs with their readable text in the user's language; names stay."""
    if not locale_bundles:
        return rows
    localized: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        copy = dict(row)
        for field in ("display_name", "description"):
            text = resolve_locale_text(
                locale_bundles,
                locale,
                f"{prefix}.{name}.{field}",
                str(row.get(field) or "").strip(),
            )
            if text:
                copy[field] = text
        localized.append(copy)
    return localized


def extension_action_operations(
    extension_id: str,
    action_definitions: Any,
    operation_manifest: Any = None,
    *,
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Workflow-visible operations; readable text follows ``locale`` when bundles exist."""
    operations: list[dict[str, Any]] = []
    workflow_actions = {
        str(row.get("action_id") or "").strip(): dict(row)
        for row in (action_definitions if isinstance(action_definitions, list) else [])
        if isinstance(row, dict)
        and row.get("workflow_visible") is True
        and str(row.get("action_id") or "").strip()
    }
    for raw_operation in operation_manifest if isinstance(operation_manifest, list) else []:
        operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
        operation_id = str(operation.get("operation_id") or "").strip()
        action = workflow_actions.get(operation_id)
        if not operation_id or action is None:
            continue
        parameters = [
            {
                **dict(item),
                "type": str(item.get("type") or item.get("value_type") or "string"),
            }
            for item in operation.get("inputs", [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("value_type") or item.get("type") or "").strip() != "connection_ref"
        ]
        declared_types = _declared_operation_connection_types(operation)
        raw_scopes = operation.get("required_oauth_scopes")
        declared_scopes = raw_scopes if isinstance(raw_scopes, (list, tuple)) else []
        returns = [
            {
                **dict(item),
                "type": str(item.get("type") or item.get("value_type") or "value"),
            }
            for item in operation.get("outputs", [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        operations.append(
            {
                "operation_id": operation_id,
                "display_name": resolve_locale_text(
                    locale_bundles,
                    locale,
                    f"op.{operation_id}.display_name",
                    str(
                        operation.get("display_name") or action.get("title") or operation_id
                    ).strip(),
                ),
                "description": resolve_locale_text(
                    locale_bundles,
                    locale,
                    f"op.{operation_id}.description",
                    str(operation.get("description") or action.get("description") or "").strip(),
                ),
                "parameters": _localized_io(
                    [item for item in parameters if isinstance(item, dict)],
                    prefix=f"op.{operation_id}.input",
                    locale=locale,
                    locale_bundles=locale_bundles,
                ),
                "returns": _localized_io(
                    returns,
                    prefix=f"op.{operation_id}.output",
                    locale=locale,
                    locale_bundles=locale_bundles,
                ),
                "source": "extension",
                "extension_id": extension_id,
                "action_id": operation_id,
                "connection_type_ids": list(declared_types),
                "connection_required": bool(declared_types),
                "required_oauth_scopes": [
                    value.strip()
                    for value in declared_scopes
                    if isinstance(value, str) and value.strip()
                ],
            }
        )
    return operations


def _release_locale_bundles(descriptor: dict[str, Any]) -> dict[str, Any]:
    bundles = descriptor.get("locale_bundles")
    return dict(bundles) if isinstance(bundles, dict) else {}


def resolve_workflow_extension_install_contract(
    connection: Any,
    *,
    install_id: str,
    account_id: str,
    default_account_id: str,
) -> dict[str, Any] | None:
    """Resolve action and optional UI contracts from one immutable install release."""
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore

    records = DBExtensionInstallStore(connection).list_for_account(
        str(account_id or default_account_id).strip() or default_account_id,
        include_global=True,
        include_inactive_owned=True,
    )
    install = next(
        (row for row in records if str(getattr(row, "install_id", "") or "").strip() == install_id),
        None,
    )
    if install is None:
        return None
    release_id = str(getattr(install, "release_id", "") or "").strip()
    if not release_id:
        return None
    release = DBExtensionRuntimeStore(connection).get_release(release_id)
    install_extension_id = str(getattr(install, "extension_id", "") or "").strip()
    if (
        release is None
        or str(getattr(release, "extension_id", "") or "").strip() != install_extension_id
    ):
        return None
    descriptor = dict(getattr(release, "descriptor", {}) or {})
    runtime_payload = descriptor.get("runtime")
    runtime = dict(runtime_payload) if isinstance(runtime_payload, dict) else {}
    contract = descriptor.get("extension_contract_v2_normalized")
    if not isinstance(contract, dict):
        contract = runtime.get("extension_contract_v2_normalized")
    if not isinstance(contract, dict):
        contract = runtime.get("extension_contract_v2")
    if isinstance(contract, dict) and any(isinstance(value, str) for value in contract.values()):
        normalized = validate_extension_contract_v2(
            descriptor,
            bundle_root=str(descriptor.get("bundle_root") or "").strip() or None,
        )
        contract = normalized.get("normalized_contract") if normalized.get("valid") else None
    if not isinstance(contract, dict):
        contract = {}
    visibility = getattr(install, "visibility", "account")
    visibility = getattr(visibility, "value", visibility)
    requested_account_id = str(account_id or default_account_id).strip() or default_account_id
    return {
        **contract,
        "account_id": (
            requested_account_id
            if str(visibility or "").strip().lower() == "global"
            else str(getattr(install, "account_id", "") or "").strip()
        ),
        "install_id": install_id,
        "extension_id": install_extension_id,
        "release_id": release_id,
        "descriptor_sha256": str(getattr(release, "descriptor_sha256", "") or "").strip().lower(),
        "install_state": str(getattr(install, "state", "active") or "active").strip().lower(),
        "action_definitions": descriptor.get("action_definitions", []),
        # This release's own dictionaries: a translation never comes from another version.
        "locale_bundles": _release_locale_bundles(descriptor),
    }


def workflow_install_connections(
    db: Any,
    *,
    project_id: str,
    install_id: str,
) -> list[dict[str, Any]]:
    list_connections = getattr(db, "list_project_connections", None)
    if not callable(list_connections):
        return []
    rows = list_connections(project_id, enabled_only=True) or []
    allowed_fields = {
        "connection_id",
        "display_name",
        "extension_id",
        "extension_install_id",
        "connection_type",
        "environment",
        "enabled",
        "health_status",
        "runtime_status",
        "oauth_requested_scopes",
        "oauth_granted_scopes",
    }
    projected: list[dict[str, Any]] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or str(row.get("extension_install_id") or "").strip() != install_id
            or not bool(row.get("enabled", True))
        ):
            continue
        item = {key: row[key] for key in allowed_fields if key in row}
        config = row.get("connection_config")
        config = dict(config) if isinstance(config, dict) else {}
        oauth = config.get("oauth")
        oauth = dict(oauth) if isinstance(oauth, dict) else {}
        verified_display_name = str(oauth.get("verified_display_name") or "").strip()
        verified_email = str(oauth.get("verified_email") or "").strip()
        if verified_display_name:
            item["verified_display_name"] = verified_display_name
        if verified_email:
            item["verified_email"] = verified_email
        lifecycle_status = str(oauth.get("lifecycle_status") or "").strip().lower()
        if lifecycle_status in _PUBLIC_OAUTH_LIFECYCLE_STATUSES:
            item["oauth_lifecycle_status"] = lifecycle_status
        projected.append(item)
    return projected
