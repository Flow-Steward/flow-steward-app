"""Projection and builder helpers for Extension Platform v2."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.registry.catalog_categories import (
    DEFAULT_CATALOG_CATEGORY,
    known_catalog_category,
)
from core.application.workflows.workflow_catalog_identity import projected_block_key

from .extension_contract_v2 import ALLOWED_WIDGET_TYPES
from .oauth_contract import (
    OAUTH_DISPLAY_MAX_LENGTH,
    OAUTH_HELP_MAX_LENGTH,
    OAUTH_PLACEHOLDER_MAX_LENGTH,
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return bool(value)


def _positive_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return parsed


def resolve_locale_string(
    locale_bundles: dict[str, dict[str, str]] | None,
    locale: str,
    key: str,
    fallback: str,
) -> str:
    """Resolve a translated string from locale bundles with en fallback."""
    from core.application.extensions.extension_locales import resolve_locale_text

    return resolve_locale_text(locale_bundles, locale, key, fallback)


def resolve_io_locale(
    items: list[dict[str, Any]],
    locale_bundles: dict[str, dict[str, str]] | None,
    locale: str,
    prefix: str,
    direction: str,
) -> list[dict[str, Any]]:
    """Resolve display_name/description on public_inputs or public_outputs."""
    if not locale_bundles or not items:
        return items
    resolved: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            resolved.append(item)
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            resolved.append(item)
            continue
        copy = dict(item)
        if "display_name" in copy:
            copy["display_name"] = resolve_locale_string(
                locale_bundles,
                locale,
                f"{prefix}.{direction}.{name}.display_name",
                str(copy["display_name"]).strip(),
            )
        if "description" in copy:
            copy["description"] = resolve_locale_string(
                locale_bundles,
                locale,
                f"{prefix}.{direction}.{name}.description",
                str(copy["description"]).strip(),
            )
        resolved.append(copy)
    return resolved


def render_oauth_provider_setup(
    *,
    provider: dict[str, Any],
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Project extension-owned OAuth Setup declarations with locale fallbacks."""
    provider_id = str(provider.get("provider_id") or "").strip()
    prefix = f"oauth.{provider_id}"
    projected = dict(provider)

    def bounded_copy(key: str, fallback: str, maximum: int) -> str:
        bounded_fallback = fallback if len(fallback) <= maximum else ""
        resolved = resolve_locale_string(locale_bundles, locale, key, bounded_fallback)
        return resolved if len(resolved) <= maximum else bounded_fallback

    projected["display_name"] = bounded_copy(
        f"{prefix}.display_name",
        str(provider.get("display_name") or "").strip(),
        OAUTH_DISPLAY_MAX_LENGTH,
    )
    projected["description"] = bounded_copy(
        f"{prefix}.description",
        str(provider.get("description") or "").strip(),
        OAUTH_HELP_MAX_LENGTH,
    )
    projected_fields: list[dict[str, Any]] = []
    for raw_field in provider.get("credential_fields") or []:
        if not isinstance(raw_field, dict):
            continue
        field = dict(raw_field)
        field_id = str(field.get("field_id") or "").strip()
        field_prefix = f"{prefix}.credential.{field_id}"
        field["label"] = bounded_copy(
            f"{field_prefix}.label",
            str(field.get("label") or "").strip(),
            OAUTH_DISPLAY_MAX_LENGTH,
        )
        field["help_text"] = bounded_copy(
            f"{field_prefix}.help_text",
            str(field.get("help_text") or "").strip(),
            OAUTH_HELP_MAX_LENGTH,
        )
        if "placeholder" in field:
            field["placeholder"] = bounded_copy(
                f"{field_prefix}.placeholder",
                str(field.get("placeholder") or "").strip(),
                OAUTH_PLACEHOLDER_MAX_LENGTH,
            )
        projected_fields.append(field)
    projected["credential_fields"] = projected_fields
    return projected


CANONICAL_EVENT_REGISTRY: set[str] = {
    "notification.requested",
    "notification.delivered",
    "notification.failed",
    "approval.requested",
    "approval.received",
    "approval.expired",
    "interaction.received",
    "job.started",
    "job.completed",
    "job.failed",
}


def _projected_block(install_id: str, local_block_key: str) -> tuple[str, str]:
    local_key = str(local_block_key or "").strip()
    if not local_key:
        raise ValueError("local_block_key is required")
    return projected_block_key(install_id, local_key), local_key


def _resolve_category(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _grounding_side_effect(
    operation: dict[str, Any], external_effects: dict[str, dict[str, Any]]
) -> str:
    operation_id = str(operation.get("operation_id") or "").strip()
    effect_kind = str(external_effects.get(operation_id, {}).get("effect_kind") or "").lower()
    operation_kind = str(
        operation.get("operation_kind") or operation.get("kind") or "action"
    ).lower()
    if operation_kind in {"source", "query", "read"}:
        return "read"
    if "delete" in effect_kind or operation_kind == "delete":
        return "delete"
    if "send" in effect_kind:
        return "send"
    return "write"


def _grounding_io(rows: Any) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        row = dict(raw) if isinstance(raw, dict) else {}
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        projected_row = {
            "name": name,
            "type": str(row.get("value_type") or row.get("type") or "value").strip() or "value",
            "required": bool(row.get("required", False)),
        }
        schema = row.get("schema")
        if isinstance(schema, dict):
            projected_row["schema"] = dict(schema)
        projected.append(projected_row)
    return projected


def build_extension_grounding_tool(
    descriptor: dict[str, Any], *, account_id: str, install_id: str
) -> dict[str, Any]:
    """Project one validated installed Contract v2 release for workflow discovery."""
    contract = descriptor.get("extension_contract_v2_normalized")
    if not isinstance(contract, dict):
        return {}
    extension_id = str(descriptor.get("extension_id") or "").strip()
    operations = [
        dict(row)
        for row in (contract.get("operation_manifest") or [])
        if isinstance(row, dict) and str(row.get("operation_id") or "").strip()
    ]
    if not extension_id or not operations:
        return {}

    declared_actions = descriptor.get("action_definitions")
    actions_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(declared_actions, list) and declared_actions:
        actions_by_id = {
            str(row.get("action_id") or row.get("operation_id") or "").strip(): dict(row)
            for row in declared_actions
            if isinstance(row, dict)
            and str(row.get("action_id") or row.get("operation_id") or "").strip()
        }
        visible_action_ids = {
            str(row.get("action_id") or row.get("operation_id") or "").strip()
            for row in declared_actions
            if isinstance(row, dict) and row.get("workflow_visible") is not False
        }
        operations = [
            row
            for row in operations
            if str(row.get("operation_id") or "").strip() in visible_action_ids
        ]
    if not operations:
        return {}

    raw_effects = descriptor.get("external_effects")
    external_effects = [dict(row) for row in raw_effects or [] if isinstance(row, dict)]
    effects_by_operation = {
        str(row.get("operation_id") or "").strip(): row
        for row in external_effects
        if str(row.get("operation_id") or "").strip()
    }
    connection_roles: list[str] = []
    for row in operations:
        for value in row.get("connection_type_ids", []):
            role = str(value).strip()
            if role and role not in connection_roles:
                connection_roles.append(role)
    listing = descriptor.get("listing")
    listing = dict(listing) if isinstance(listing, dict) else {}
    recipes = descriptor.get("operation_recipes")
    if not isinstance(recipes, list):
        recipes = contract.get("operation_recipes")
    artifact_policies = contract.get("artifact_policies")
    return {
        "tool_id": f"extension.{extension_id}",
        "display_name": str(descriptor.get("display_name") or extension_id).strip(),
        "description": str(descriptor.get("description") or "").strip(),
        "enabled": True,
        "visible_in_catalog": True,
        "category": known_catalog_category(listing.get("category")) or DEFAULT_CATALOG_CATEGORY,
        "tags": [str(value) for value in listing.get("tags") or [] if str(value).strip()],
        "scope_kind": "account",
        "account_id": str(account_id or "").strip(),
        "source_install_id": str(install_id or "").strip(),
        "auth": {
            "auth_source": "project_connection" if connection_roles else "",
            "required_connection_roles": connection_roles,
        },
        "interface": {
            "operations": [
                {
                    "operation_id": str(operation.get("operation_id") or "").strip(),
                    "display_name": str(
                        operation.get("display_name") or operation.get("operation_id") or ""
                    ).strip(),
                    "description": str(operation.get("description") or "").strip(),
                    "parameters": _grounding_io(operation.get("inputs")),
                    "returns": _grounding_io(operation.get("outputs")),
                    "side_effect_type": _grounding_side_effect(operation, effects_by_operation),
                    "idempotency": str(
                        operation.get("idempotency")
                        or effects_by_operation.get(
                            str(operation.get("operation_id") or "").strip(), {}
                        ).get("idempotency")
                        or ""
                    ).strip(),
                    "operation_kind": str(operation.get("operation_kind") or "").strip(),
                    "connection_type_ids": [
                        str(value).strip()
                        for value in operation.get("connection_type_ids", [])
                        if str(value).strip()
                    ],
                    **(
                        {"mutates_platform": bool(action.get("mutates_platform"))}
                        if "mutates_platform" in action
                        else {}
                    ),
                }
                for operation in operations
                for action in [
                    actions_by_id.get(str(operation.get("operation_id") or "").strip(), {})
                ]
            ]
        },
        "external_effects": external_effects,
        "artifact_policies": [
            dict(row) for row in artifact_policies or [] if isinstance(row, dict)
        ],
        "operation_recipes": [dict(row) for row in recipes or [] if isinstance(row, dict)],
    }


def installed_extension_grounding_tools(db: Any, *, account_id: str) -> list[dict[str, Any]]:
    """Return release-bound Contract v2 tools visible to an account."""
    install_store = getattr(db, "extension_install_store", None)
    runtime_store = getattr(db, "extension_runtime_store", None)
    if install_store is None or runtime_store is None:
        dsn = str(getattr(db, "dsn", "") or "").strip()
        if not dsn:
            return []
        try:
            from core.application.extensions.service_support import (
                get_default_extension_install_store,
                get_default_extension_runtime_store,
            )
            from core.infrastructure.db.platform_stores import PlatformIdentityStore

            identity_store = PlatformIdentityStore(dsn)
            install_store = get_default_extension_install_store(get_logger=lambda: identity_store)
            runtime_store = get_default_extension_runtime_store(get_logger=lambda: identity_store)
        except Exception:
            return []
    try:
        installs = install_store.list_for_account(
            str(account_id or "").strip(),
            include_global=True,
            include_inactive_owned=False,
        )
    except Exception:
        return []
    tools_by_id: dict[str, tuple[bool, dict[str, Any]]] = {}
    for install in installs or []:
        release_id = str(getattr(install, "release_id", "") or "").strip()
        if not release_id:
            continue
        try:
            release = runtime_store.get_release(release_id)
        except Exception:
            continue
        if release is None:
            continue
        descriptor = dict(getattr(release, "descriptor", {}) or {})
        if (
            str(descriptor.get("extension_id") or "").strip()
            != str(getattr(install, "extension_id", "") or "").strip()
        ):
            continue
        tool = build_extension_grounding_tool(
            descriptor,
            account_id=str(account_id or "").strip(),
            install_id=str(getattr(install, "install_id", "") or "").strip(),
        )
        if tool:
            tool_id = str(tool.get("tool_id") or "").strip()
            owned = (
                str(getattr(install, "account_id", "") or "").strip()
                == str(account_id or "").strip()
            )
            previous = tools_by_id.get(tool_id)
            if tool_id and (previous is None or (owned and not previous[0])):
                tools_by_id[tool_id] = (owned, tool)
    return [tools_by_id[tool_id][1] for tool_id in sorted(tools_by_id)]


def project_contract_to_registry(
    contract: dict[str, list[dict[str, Any]]],
    *,
    account_id: str,
    install_id: str,
    release_id: str,
    enabled: bool = True,
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Project operation contracts into deterministic derived rows."""
    sync_status = "active" if enabled else "disabled"

    blocks: list[dict[str, Any]] = []
    connection_types: list[dict[str, Any]] = []

    for ct in contract.get("connection_types", []):
        ct_id = str(ct.get("connection_type_id") or "").strip()
        if not ct_id:
            continue
        connection_types.append(
            {
                **dict(ct.items()),
                "display_name": resolve_locale_string(
                    locale_bundles,
                    locale,
                    f"connection.{ct_id}.display_name",
                    str(ct.get("display_name") or ct_id).strip(),
                ),
                "description": resolve_locale_string(
                    locale_bundles,
                    locale,
                    f"connection.{ct_id}.description",
                    str(ct.get("description") or "").strip(),
                ),
            }
        )

    for operation in contract.get("operation_manifest", []):
        operation_id = str(operation.get("operation_id") or "").strip()
        if not operation_id:
            continue
        operation_kind = str(
            operation.get("operation_kind") or operation.get("kind") or "action"
        ).strip()
        supports_batch = _as_bool(operation.get("supports_batch"))
        preferred_batch_size = _positive_int(operation.get("preferred_batch_size"))
        max_batch_size = _positive_int(operation.get("max_batch_size"))
        batch_input_field = str(operation.get("batch_input_field") or "").strip()
        partial_failure_mode = str(operation.get("partial_failure_mode") or "").strip()
        raw_supports_async_job = operation.get("supports_async_job")
        if isinstance(raw_supports_async_job, bool):
            supports_async_job = raw_supports_async_job
        elif supports_batch:
            supports_async_job = _as_bool(raw_supports_async_job)
        else:
            supports_async_job = False

        binding_payload: dict[str, Any] = {
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "connection_type_ids": [
                str(value).strip()
                for value in operation.get("connection_type_ids", [])
                if str(value).strip()
            ],
            "supports_batch": supports_batch,
        }
        if supports_batch:
            if preferred_batch_size is not None:
                binding_payload["preferred_batch_size"] = preferred_batch_size
            if max_batch_size is not None:
                binding_payload["max_batch_size"] = max_batch_size
            if batch_input_field:
                binding_payload["batch_input_field"] = batch_input_field
            if partial_failure_mode:
                binding_payload["partial_failure_mode"] = partial_failure_mode
            binding_payload["supports_async_job"] = bool(supports_async_job)

        operation_local_block_key = f"operation.{operation_id}"
        operation_block_key, local_operation_block_key = _projected_block(
            install_id, operation_local_block_key
        )
        blocks.append(
            {
                "account_id": account_id,
                "install_id": install_id,
                "release_id": release_id,
                "block_key": operation_block_key,
                "local_block_key": local_operation_block_key,
                "source_install_id": install_id,
                "source_release_id": release_id,
                "display_name": resolve_locale_string(
                    locale_bundles,
                    locale,
                    f"op.{operation_id}.display_name",
                    str(operation.get("display_name") or operation_id).strip(),
                ),
                "block_type": "operation",
                "source_kind": "operation",
                "source_ref": operation_id,
                "family": _resolve_category(operation.get("category"), operation.get("family_id")),
                "binding": binding_payload,
                "projection_mode": "derived",
                "sync_status": sync_status,
                "enabled": bool(enabled),
            }
        )

    blocks.extend(
        project_trigger_blocks(
            contract,
            account_id=account_id,
            install_id=install_id,
            release_id=release_id,
            enabled=enabled,
            locale=locale,
            locale_bundles=locale_bundles,
        )
    )

    blocks.sort(key=lambda row: row["block_key"])
    connection_types.sort(key=lambda row: str(row.get("connection_type_id") or ""))
    return {
        "families": [],
        "skills": [],
        "blocks": blocks,
        "connection_types": connection_types,
    }


def reconcile_derived_rows(
    *,
    previous_rows: list[dict[str, Any]],
    projected_rows: list[dict[str, Any]],
    key_field: str,
) -> list[dict[str, Any]]:
    """Mark no-longer-projected rows as orphaned and disabled."""
    current_by_key = {
        str(row.get(key_field) or "").strip(): row
        for row in projected_rows
        if str(row.get(key_field) or "").strip()
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in projected_rows:
        key = str(row.get(key_field) or "").strip()
        if not key or key in seen:
            continue
        merged.append(dict(row))
        seen.add(key)

    for old in previous_rows:
        key = str(old.get(key_field) or "").strip()
        if not key or key in current_by_key or key in seen:
            continue
        orphaned = dict(old)
        orphaned["sync_status"] = "orphaned"
        orphaned["enabled"] = False
        merged.append(orphaned)
        seen.add(key)

    merged.sort(key=lambda row: str(row.get(key_field) or ""))
    return merged


def build_builder_catalog(
    *,
    contract: dict[str, list[dict[str, Any]]],
    projected_blocks: list[dict[str, Any]],
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return normalized builder catalog entries from projected blocks only."""
    operations = {
        str(row.get("operation_id") or "").strip(): row
        for row in contract.get("operation_manifest", [])
        if str(row.get("operation_id") or "").strip()
    }

    entries: list[dict[str, Any]] = []
    for block in projected_blocks:
        if not bool(block.get("enabled", True)):
            continue
        if str(block.get("sync_status") or "active") != "active":
            continue
        block_key = str(block.get("block_key") or "").strip()
        if not block_key:
            continue
        block_type = str(block.get("block_type") or "").strip()
        binding = dict(block.get("binding") or {})

        public_inputs: list[dict[str, Any]] = []
        public_outputs: list[dict[str, Any]] = []
        validation_hints: dict[str, Any] = {}
        if block_type == "operation":
            operation_id = str(binding.get("operation_id") or "").strip()
            operation = operations.get(operation_id, {})
            supports_batch = _as_bool(operation.get("supports_batch"))
            operation_kind = str(
                operation.get("operation_kind") or operation.get("kind") or "action"
            ).strip()
            public_inputs = resolve_io_locale(
                list(operation.get("inputs") or operation.get("public_inputs") or []),
                locale_bundles,
                locale,
                f"op.{operation_id}",
                "input",
            )
            public_outputs = resolve_io_locale(
                list(operation.get("outputs") or operation.get("public_outputs") or []),
                locale_bundles,
                locale,
                f"op.{operation_id}",
                "output",
            )
            validation_hints = {
                "operation_kind": operation_kind,
                "supports_batch": supports_batch,
            }
            if supports_batch:
                preferred_batch_size = _positive_int(operation.get("preferred_batch_size"))
                max_batch_size = _positive_int(operation.get("max_batch_size"))
                batch_input_field = str(operation.get("batch_input_field") or "").strip()
                partial_failure_mode = str(operation.get("partial_failure_mode") or "").strip()
                supports_async_job = (
                    operation.get("supports_async_job")
                    if isinstance(operation.get("supports_async_job"), bool)
                    else _as_bool(operation.get("supports_async_job"))
                )
                if preferred_batch_size is not None:
                    validation_hints["preferred_batch_size"] = preferred_batch_size
                if max_batch_size is not None:
                    validation_hints["max_batch_size"] = max_batch_size
                if batch_input_field:
                    validation_hints["batch_input_field"] = batch_input_field
                if partial_failure_mode:
                    validation_hints["partial_failure_mode"] = partial_failure_mode
                validation_hints["supports_async_job"] = bool(supports_async_job)
        entries.append(
            {
                "block_key": block_key,
                "display_name": str(block.get("display_name") or block_key).strip(),
                "block_type": block_type,
                "public_inputs": public_inputs,
                "public_outputs": public_outputs,
                "validation_hints": validation_hints,
                "connection_requirements": {
                    "connection_type_ids": list(binding.get("connection_type_ids") or [])
                },
                "runtime_binding": binding,
            }
        )

    entries.sort(key=lambda row: row["block_key"])
    return entries


def render_step_form(
    *,
    contract: dict[str, list[dict[str, Any]]],
    operation_id: str,
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Render Flow Steward-owned step form schema from step_ui_manifest."""
    op_id = str(operation_id or "").strip()
    step_rows = [
        row
        for row in contract.get("step_ui_manifest", [])
        if str(row.get("operation_id") or "").strip() == op_id
    ]
    if not step_rows:
        return {
            "operation_id": op_id,
            "sections": [],
            "errors": [f"Missing step_ui_manifest for '{op_id}'"],
        }

    rendered_sections: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in step_rows:
        section_name = resolve_locale_string(
            locale_bundles,
            locale,
            f"step.{op_id}.section",
            str(row.get("section") or row.get("title") or "General").strip(),
        )
        rendered_fields: list[dict[str, Any]] = []
        for field in row.get("fields") or []:
            if not isinstance(field, dict):
                continue
            widget = (
                str(field.get("widget") or field.get("widget_type") or field.get("type") or "")
                .strip()
                .lower()
            )
            if widget not in ALLOWED_WIDGET_TYPES:
                errors.append(
                    f"Field '{field.get('name')}' has unsupported widget '{widget}' for operation '{op_id}'"
                )
                continue
            field_name = str(field.get("name") or "").strip()
            rendered_fields.append(
                {
                    "name": field_name,
                    "label": resolve_locale_string(
                        locale_bundles,
                        locale,
                        f"step.{op_id}.field.{field_name}.label",
                        str(field.get("label") or field.get("name") or "").strip(),
                    ),
                    "widget_type": widget,
                    "help_text": resolve_locale_string(
                        locale_bundles,
                        locale,
                        f"step.{op_id}.field.{field_name}.help_text",
                        str(field.get("help_text") or "").strip(),
                    ),
                    "placeholder": resolve_locale_string(
                        locale_bundles,
                        locale,
                        f"step.{op_id}.field.{field_name}.placeholder",
                        str(field.get("placeholder") or "").strip(),
                    ),
                    "required": bool(field.get("required", False)),
                    "depends_on": dict(field.get("depends_on") or {}),
                    "query_id": str(
                        field.get("query_id")
                        or field.get("resource_query_id")
                        or field.get("picker_query_id")
                        or ""
                    ).strip(),
                    "options": [
                        dict(option)
                        for option in field.get("options") or []
                        if isinstance(option, dict)
                    ],
                    **(
                        {"external_resource_picker": dict(field["external_resource_picker"])}
                        if isinstance(field.get("external_resource_picker"), dict)
                        else {}
                    ),
                }
            )
        rendered_sections.append({"section": section_name, "fields": rendered_fields})

    return {
        "operation_id": op_id,
        "sections": rendered_sections,
        "errors": errors,
    }


def execute_resource_query(
    *,
    contract: dict[str, list[dict[str, Any]]],
    query_id: str,
    provider: Callable[[dict[str, Any]], dict[str, Any]],
    account_id: str,
    project_id: Any,
    connection_id: str,
    search: str = "",
    cursor: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Execute one validated resource query with scoped context."""
    qid = str(query_id or "").strip()
    query = None
    for row in contract.get("resource_query_manifest", []):
        if str(row.get("query_id") or "").strip() == qid:
            query = row
            break
    if not query:
        return {"ok": False, "error": f"Unknown query_id '{qid}'", "items": [], "next_cursor": ""}

    request_payload = {
        "query_id": qid,
        "search": str(search or "").strip(),
        "cursor": str(cursor or "").strip(),
        "limit": max(1, min(int(limit or 50), 500)),
        "scope": {
            "account_id": str(account_id or "").strip(),
            "project_id": str(project_id or "").strip(),
            "connection_id": str(connection_id or "").strip(),
        },
        "query_manifest": dict(query),
    }
    response = provider(request_payload)
    if not isinstance(response, dict):
        return {
            "ok": False,
            "error": "Provider returned non-object response",
            "items": [],
            "next_cursor": "",
        }
    items = response.get("items")
    if not isinstance(items, list):
        items = []
    next_cursor = str(response.get("next_cursor") or "").strip()
    return {
        "ok": True,
        "items": items,
        "next_cursor": next_cursor,
        "total": int(response.get("total") or len(items)),
    }


def validate_canonical_event_bindings(
    contract: dict[str, list[dict[str, Any]]],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for idx, binding in enumerate(contract.get("event_bindings", [])):
        event_name = str(
            binding.get("event_name")
            or binding.get("canonical_event")
            or binding.get("event_type")
            or ""
        ).strip()
        if not event_name:
            continue
        if event_name not in CANONICAL_EVENT_REGISTRY:
            errors.append(
                {
                    "path": f"event_bindings[{idx}].event_name",
                    "message": f"Unknown canonical event '{event_name}'",
                }
            )
    return errors


def map_provider_event_to_canonical(
    *,
    provider_event: str,
    mapping_rules: dict[str, str],
) -> str:
    provider_key = str(provider_event or "").strip().lower()
    canonical = str(mapping_rules.get(provider_key) or "").strip()
    return canonical or provider_key


def project_trigger_blocks(
    contract: dict[str, list[dict[str, Any]]],
    *,
    account_id: str,
    install_id: str,
    release_id: str,
    enabled: bool = True,
    locale: str = "",
    locale_bundles: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    sync_status = "active" if enabled else "disabled"
    for binding in contract.get("event_bindings", []):
        trigger_id = str(binding.get("trigger_id") or "").strip()
        event_name = str(
            binding.get("event_name")
            or binding.get("canonical_event")
            or binding.get("event_type")
            or ""
        ).strip()
        if not trigger_id or not event_name:
            continue
        trigger_local_block_key = f"trigger.{trigger_id}"
        trigger_block_key, local_trigger_block_key = _projected_block(
            install_id, trigger_local_block_key
        )
        blocks.append(
            {
                "account_id": account_id,
                "install_id": install_id,
                "release_id": release_id,
                "block_key": trigger_block_key,
                "local_block_key": local_trigger_block_key,
                "source_install_id": install_id,
                "source_release_id": release_id,
                "display_name": resolve_locale_string(
                    locale_bundles,
                    locale,
                    f"trigger.{trigger_id}.display_name",
                    str(binding.get("display_name") or trigger_id).strip(),
                ),
                "block_type": "trigger",
                "source_kind": "event_binding",
                "source_ref": trigger_id,
                "family": "trigger",
                "binding": {
                    "trigger_id": trigger_id,
                    "event_name": event_name,
                    "filters": dict(binding.get("filters") or {}),
                    "idempotency_hint": str(binding.get("idempotency_hint") or "").strip(),
                },
                "projection_mode": "derived",
                "sync_status": sync_status,
                "enabled": bool(enabled),
            }
        )
    blocks.sort(key=lambda row: row["block_key"])
    return blocks


def human_channel_event_flow(
    *,
    channel: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical event flow helper for notification/approval loops."""
    channel_name = str(channel or "").strip().lower() or "unknown"
    action_name = str(action or "").strip().lower()
    event = "interaction.received"
    if action_name == "request_approval":
        event = "approval.requested"
    elif action_name == "approval_response":
        event = "approval.received"
    elif action_name == "notify":
        event = "notification.requested"
    return {
        "channel": channel_name,
        "action": action_name,
        "canonical_event": event,
        "payload": dict(payload or {}),
    }
