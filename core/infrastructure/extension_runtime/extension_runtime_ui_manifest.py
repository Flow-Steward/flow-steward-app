# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

from .extension_browser_picker_contract import _validate_external_resource_picker
from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_manifest import _resolve_object_ref
from .extension_runtime_validation import (
    _as_dict,
    _as_list,
    _issue,
    _normalize_string_list,
)


def _resolve_component_block(
    *,
    block: Any,
    bundle_root: Path,
    source_path: Path,
    issues: list[dict],
    field_path: str,
    component_ids: set[str],
    page_id: str,
    ref_stack: set[str],
) -> Any:
    if not isinstance(block, dict):
        return block
    payload = dict(block)

    for ref_field, (resolved_field, invalid_code) in COMPONENT_REF_OBJECT_FIELDS.items():
        ref_text = str(payload.get(ref_field) or "").strip()
        if not ref_text:
            continue
        resolved_data, resolved_path = _resolve_object_ref(
            bundle_root=bundle_root,
            parent_path=source_path,
            ref_text=ref_text,
            issues=issues,
            field_path=f"{field_path}.{ref_field}",
        )
        if resolved_data is None:
            continue
        if not isinstance(resolved_data, dict):
            issues.append(
                _issue(
                    severity="error",
                    code=invalid_code,
                    message=f"{ref_field} must resolve to an object",
                    file_path=str(resolved_path or source_path),
                    field_path=f"{field_path}.{ref_field}",
                )
            )
            continue
        payload[resolved_field] = resolved_data

    component_id = str(payload.get("component_id") or "").strip()
    if component_id:
        if component_id in component_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_component_id",
                    message=f"Duplicate component_id '{component_id}'",
                    file_path=str(source_path),
                    field_path=field_path,
                    hint=f"Use unique component_id values per extension (page '{page_id}').",
                )
            )
        else:
            component_ids.add(component_id)

    if "components" in payload and isinstance(payload.get("components"), list):
        resolved_children: list[Any] = []
        for idx, child in enumerate(payload.get("components") or []):
            child_field_path = f"{field_path}.components[{idx}]"
            if isinstance(child, dict) and str(child.get("ref") or "").strip():
                child_ref = str(child.get("ref") or "").strip()
                child_data, child_path = _resolve_object_ref(
                    bundle_root=bundle_root,
                    parent_path=source_path,
                    ref_text=child_ref,
                    issues=issues,
                    field_path=child_field_path,
                )
                if child_data is None:
                    continue
                if not isinstance(child_data, dict):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_component_ref",
                            message=f"Component ref '{child_ref}' must resolve to an object",
                            file_path=str(source_path),
                            field_path=child_field_path,
                        )
                    )
                    continue
                if child_path in ref_stack:
                    issues.append(
                        _issue(
                            severity="error",
                            code="circular_ref",
                            message=f"Circular component ref detected for '{child_ref}'",
                            file_path=child_path,
                            field_path=child_field_path,
                        )
                    )
                    continue
                nested_stack = set(ref_stack)
                nested_stack.add(child_path)
                resolved_children.append(
                    _resolve_component_block(
                        block=child_data,
                        bundle_root=bundle_root,
                        source_path=Path(child_path),
                        issues=issues,
                        field_path=child_field_path,
                        component_ids=component_ids,
                        page_id=page_id,
                        ref_stack=nested_stack,
                    )
                )
            else:
                resolved_children.append(
                    _resolve_component_block(
                        block=child,
                        bundle_root=bundle_root,
                        source_path=source_path,
                        issues=issues,
                        field_path=child_field_path,
                        component_ids=component_ids,
                        page_id=page_id,
                        ref_stack=set(ref_stack),
                    )
                )
        payload["components"] = resolved_children
    _validate_component_fields(
        component=payload,
        issues=issues,
        source_path=source_path,
        field_path=field_path,
    )
    return payload


def _validate_component_fields(
    *,
    component: dict[str, Any],
    issues: list[dict],
    source_path: Path,
    field_path: str,
) -> None:
    if str(component.get("type") or "").strip() == "connection_form":
        _validate_connection_form_pre_save_actions(
            component=component,
            issues=issues,
            source_path=source_path,
            field_path=field_path,
        )
    fields = component.get("fields")
    if not isinstance(fields, list):
        return
    for idx, raw_field in enumerate(fields):
        if not isinstance(raw_field, dict):
            continue
        widget = str(raw_field.get("widget") or raw_field.get("type") or "").strip().lower()
        if widget != "select":
            continue
        props = _as_dict(raw_field.get("props"))
        options = raw_field.get("options")
        if not isinstance(options, list) or not options:
            options = props.get("options")
        if isinstance(options, list) and options:
            seen_values: set[str] = set()
            for option_idx, option in enumerate(options):
                if not isinstance(option, dict):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_select_option",
                            message="Select options must be objects with value and label.",
                            file_path=str(source_path),
                            field_path=f"{field_path}.fields[{idx}].options[{option_idx}]",
                        )
                    )
                    continue
                value = str(option.get("value") or "").strip()
                label = str(option.get("label") or "").strip()
                if not value:
                    issues.append(
                        _issue(
                            severity="error",
                            code="missing_select_option_value",
                            message="Select option value is required.",
                            file_path=str(source_path),
                            field_path=f"{field_path}.fields[{idx}].options[{option_idx}].value",
                        )
                    )
                elif value in seen_values:
                    issues.append(
                        _issue(
                            severity="error",
                            code="duplicate_select_option_value",
                            message="Select option values must be unique.",
                            file_path=str(source_path),
                            field_path=f"{field_path}.fields[{idx}].options[{option_idx}].value",
                        )
                    )
                else:
                    seen_values.add(value)
                if not label:
                    issues.append(
                        _issue(
                            severity="error",
                            code="missing_select_option_label",
                            message="Select option label is required.",
                            file_path=str(source_path),
                            field_path=f"{field_path}.fields[{idx}].options[{option_idx}].label",
                        )
                    )
            continue
        issues.append(
            _issue(
                severity="error",
                code="missing_select_options",
                message="Select fields must declare non-empty options or props.options.",
                file_path=str(source_path),
                field_path=f"{field_path}.fields[{idx}].options",
            )
        )


def _connection_form_actions(data: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        _as_dict(item)
        for item in _as_list(data.get("pre_save_actions") or data.get("preSaveActions"))
    ]
    if actions:
        return [item for item in actions if item]
    single = _as_dict(data.get("pre_save_action") or data.get("preSaveAction"))
    return [single] if single else []


def _connection_form_field_path(field: Any) -> str:
    field_payload = _as_dict(field)
    return str(
        field_payload.get("path")
        or field_payload.get("name")
        or field_payload.get("field_id")
        or field_payload.get("id")
        or ""
    ).strip()


def _connection_form_secret_field_paths(component: dict[str, Any]) -> set[str]:
    data = _as_dict(component.get("data"))
    secret_fields = set(_normalize_string_list(data.get("secret_fields")))
    secret_fields.update(_normalize_string_list(data.get("required_secret_fields")))
    variants = _as_dict(data.get("connection_types") or data.get("variants"))
    for variant in variants.values():
        variant_data = _as_dict(variant)
        secret_fields.update(_normalize_string_list(variant_data.get("secret_fields")))
        secret_fields.update(_normalize_string_list(variant_data.get("required_secret_fields")))
    for field in _as_list(component.get("fields")):
        field_payload = _as_dict(field)
        path = _connection_form_field_path(field_payload)
        widget = str(field_payload.get("widget") or field_payload.get("type") or "").strip().lower()
        if path and (widget == "secret" or field_payload.get("secret") is True):
            secret_fields.add(path)
    return secret_fields


def _connection_form_config_field_paths(component: dict[str, Any]) -> set[str]:
    data = _as_dict(component.get("data"))
    config_fields = set(_normalize_string_list(data.get("config_fields")))
    variants = _as_dict(data.get("connection_types") or data.get("variants"))
    for variant in variants.values():
        config_fields.update(_normalize_string_list(_as_dict(variant).get("config_fields")))
    return config_fields


def _validate_connection_form_pre_save_actions(
    *,
    component: dict[str, Any],
    issues: list[dict],
    source_path: Path,
    field_path: str,
) -> None:
    data = _as_dict(component.get("data"))
    config_fields = _connection_form_config_field_paths(component)
    secret_fields = _connection_form_secret_field_paths(component)
    for idx, action in enumerate(_connection_form_actions(data)):
        result_mapping = _as_dict(action.get("result_mapping") or action.get("resultMapping"))
        if not result_mapping:
            continue
        for target_field in result_mapping:
            target = str(target_field or "").strip()
            if not target:
                continue
            mapping_path = f"{field_path}.data.pre_save_actions[{idx}].result_mapping.{target}"
            if target in secret_fields:
                issues.append(
                    _issue(
                        severity="error",
                        code="secret_pre_save_result_mapping_target",
                        message=(
                            "pre_save_actions result_mapping targets must not write to "
                            f"secret field '{target}'"
                        ),
                        file_path=str(source_path),
                        field_path=mapping_path,
                    )
                )
            elif target not in config_fields:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_pre_save_result_mapping_target",
                        message=(
                            "pre_save_actions result_mapping targets must be declared "
                            f"non-secret config_fields; '{target}' is not declared"
                        ),
                        file_path=str(source_path),
                        field_path=mapping_path,
                    )
                )


def _resolve_ui_manifest(
    *,
    bundle_root: Path,
    manifest_path: Path,
    ui_manifest_ref: str,
    issues: list[dict],
) -> dict:
    if not ui_manifest_ref:
        issues.append(
            _issue(
                severity="error",
                code="missing_ui_manifest",
                message="Manifest v2 requires ui_manifest ref",
                file_path=str(manifest_path),
                field_path="ui_manifest",
                hint="Add ui_manifest: ui/ui_manifest.yaml (or another valid relative path).",
            )
        )
        return {}

    root_data, ui_manifest_path = _resolve_object_ref(
        bundle_root=bundle_root,
        parent_path=manifest_path,
        ref_text=ui_manifest_ref,
        issues=issues,
        field_path="ui_manifest",
    )
    if root_data is None:
        return {"version": "1", "pages": []}
    if not isinstance(root_data, dict):
        issues.append(
            _issue(
                severity="error",
                code="invalid_ui_manifest",
                message="ui_manifest must resolve to an object",
                file_path=ui_manifest_path,
                field_path="ui_manifest",
            )
        )
        return {"version": "1", "pages": []}

    pages = root_data.get("pages")
    if not isinstance(pages, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_ui_pages",
                message="ui_manifest.pages must be a list",
                file_path=ui_manifest_path,
                field_path="pages",
            )
        )
        pages = []

    seen_page_ids: set[str] = set()
    seen_component_ids: set[str] = set()
    resolved_pages: list[dict] = []

    for idx, page in enumerate(pages or []):
        page_field_path = f"pages[{idx}]"
        if isinstance(page, dict) and str(page.get("ref") or "").strip():
            page_ref = str(page.get("ref") or "").strip()
            loaded_page, page_path = _resolve_object_ref(
                bundle_root=bundle_root,
                parent_path=Path(ui_manifest_path),
                ref_text=page_ref,
                issues=issues,
                field_path=page_field_path,
            )
            if loaded_page is None:
                continue
            if not isinstance(loaded_page, dict):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_page_ref",
                        message=f"Page ref '{page_ref}' must resolve to an object",
                        file_path=str(ui_manifest_path),
                        field_path=page_field_path,
                    )
                )
                continue
            page = loaded_page
            page_source_path = Path(page_path)
        elif isinstance(page, dict):
            page_source_path = Path(ui_manifest_path)
        else:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_page",
                    message="Each page must be an object",
                    file_path=ui_manifest_path,
                    field_path=page_field_path,
                )
            )
            continue
        page_id = str(page.get("page_id") or "").strip()
        if not page_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_page_id",
                    message="Page page_id is required",
                    file_path=ui_manifest_path,
                    field_path=f"{page_field_path}.page_id",
                )
            )
            continue
        if page_id in seen_page_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_page_id",
                    message=f"Duplicate page_id '{page_id}'",
                    file_path=ui_manifest_path,
                    field_path=f"{page_field_path}.page_id",
                )
            )
            continue
        seen_page_ids.add(page_id)

        resolved_components: list[dict[str, Any]] = []
        resolved_page: dict[str, Any] = {
            "page_id": page_id,
            "title": str(page.get("title") or "").strip() or page_id,
            "description": str(page.get("description") or "").strip(),
            "project_scoped": bool(page.get("project_scoped", True)),
            "components": resolved_components,
        }

        components = page.get("components")
        if not isinstance(components, list):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_page_components",
                    message="Page components must be a list",
                    file_path=ui_manifest_path,
                    field_path=f"{page_field_path}.components",
                )
            )
            components = []

        for cidx, component in enumerate(components or []):
            component_field_path = f"{page_field_path}.components[{cidx}]"
            if isinstance(component, dict) and str(component.get("ref") or "").strip():
                ref_text = str(component.get("ref") or "").strip()
                loaded, component_path = _resolve_object_ref(
                    bundle_root=bundle_root,
                    parent_path=page_source_path,
                    ref_text=ref_text,
                    issues=issues,
                    field_path=component_field_path,
                )
                if loaded is None:
                    continue
                if not isinstance(loaded, dict):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_component_ref",
                            message=f"Component ref '{ref_text}' must resolve to an object",
                            file_path=str(ui_manifest_path),
                            field_path=component_field_path,
                        )
                    )
                    continue
                resolved_components.append(
                    _resolve_component_block(
                        block=loaded,
                        bundle_root=bundle_root,
                        source_path=Path(component_path),
                        issues=issues,
                        field_path=component_field_path,
                        component_ids=seen_component_ids,
                        page_id=page_id,
                        ref_stack={str(component_path)},
                    )
                )
            else:
                resolved_components.append(
                    _resolve_component_block(
                        block=component,
                        bundle_root=bundle_root,
                        source_path=page_source_path,
                        issues=issues,
                        field_path=component_field_path,
                        component_ids=seen_component_ids,
                        page_id=page_id,
                        ref_stack=set(),
                    )
                )

        resolved_pages.append(resolved_page)

    return {
        "version": str(root_data.get("version") or "1").strip() or "1",
        "pages": resolved_pages,
    }


def _validate_scope_shape(scope: str) -> bool:
    text = str(scope or "").strip()
    if not text or " " in text:
        return False
    if ":" not in text:
        return False
    left, _, right = text.partition(":")
    return bool(left and right)


def _validate_scope_list(
    *,
    issues: list[dict],
    scopes_value: Any,
    manifest_path: str,
    field_path: str,
) -> None:
    for idx, scope in enumerate(_normalize_string_list(scopes_value)):
        if _validate_scope_shape(scope):
            continue
        issues.append(
            _issue(
                severity="error",
                code="invalid_scope",
                message=f"Invalid scope '{scope}'",
                file_path=manifest_path,
                field_path=f"{field_path}[{idx}]",
                hint="Use scope format like 'event:subscribe' or 'command:resume_job'.",
            )
        )


def _component_index(components: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def visit(rows: Any) -> None:
        for raw in _as_list(rows):
            component = _as_dict(raw)
            component_id = str(component.get("component_id") or "").strip()
            if component_id:
                result[component_id] = component
            visit(component.get("components"))

    visit(components)
    return result


def _walk_component_tree(
    value: Any,
    *,
    manifest_path: str,
    base_path: str,
    query_ids: set[str],
    action_ids: set[str],
    action_parameters: dict[str, set[str]] | None,
    issues: list[dict],
    in_component_slot: bool = False,
    component_index: dict[str, dict[str, Any]] | None = None,
    browser_bootstrap_action_ids: set[str] | None = None,
) -> None:
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _walk_component_tree(
                item,
                manifest_path=manifest_path,
                base_path=f"{base_path}[{idx}]",
                query_ids=query_ids,
                action_ids=action_ids,
                action_parameters=action_parameters,
                issues=issues,
                in_component_slot=in_component_slot,
                component_index=component_index,
                browser_bootstrap_action_ids=browser_bootstrap_action_ids,
            )
        return
    if not isinstance(value, dict):
        return

    component_type = str(value.get("type") or "").strip()
    has_component_identity = bool(
        str(value.get("component_id") or "").strip() or "components" in value or in_component_slot
    )
    if has_component_identity:
        if not component_type:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_component_type",
                    message="Component type is required",
                    file_path=manifest_path,
                    field_path=f"{base_path}.type",
                )
            )
        elif component_type == "form":
            ui_schema = value.get("ui_schema")
            if not isinstance(ui_schema, dict):
                issues.append(
                    _issue(
                        severity="error",
                        code="missing_form_ui_schema",
                        message="Form components must define ui_schema",
                        file_path=manifest_path,
                        field_path=f"{base_path}.ui_schema",
                    )
                )
            else:
                fields = ui_schema.get("fields")
                if not isinstance(fields, list):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_form_ui_schema_fields",
                            message="ui_schema.fields must be a list",
                            file_path=manifest_path,
                            field_path=f"{base_path}.ui_schema.fields",
                        )
                    )
                else:
                    for field_idx, field_payload in enumerate(fields):
                        if not isinstance(field_payload, dict):
                            issues.append(
                                _issue(
                                    severity="error",
                                    code="invalid_form_field",
                                    message="Form ui_schema field must be an object",
                                    file_path=manifest_path,
                                    field_path=f"{base_path}.ui_schema.fields[{field_idx}]",
                                )
                            )
                            continue
                        widget = str(field_payload.get("widget") or "").strip().lower()
                        if not widget:
                            issues.append(
                                _issue(
                                    severity="error",
                                    code="missing_form_field_widget",
                                    message="Form field widget is required",
                                    file_path=manifest_path,
                                    field_path=f"{base_path}.ui_schema.fields[{field_idx}].widget",
                                )
                            )
                        elif widget not in ALLOWED_FORM_WIDGET_TYPES:
                            issues.append(
                                _issue(
                                    severity="error",
                                    code="unsupported_form_widget",
                                    message=f"Unsupported form widget '{widget}'",
                                    file_path=manifest_path,
                                    field_path=f"{base_path}.ui_schema.fields[{field_idx}].widget",
                                )
                            )
            if isinstance(value.get("fields"), list):
                issues.append(
                    _issue(
                        severity="error",
                        code="top_level_form_fields_not_supported",
                        message="Form fields must be declared in ui_schema.fields, not top-level fields",
                        file_path=manifest_path,
                        field_path=f"{base_path}.fields",
                    )
                )
            form_fields = {
                str(field.get("path") or field.get("field_id") or "").strip()
                for field in _as_list(_as_dict(value.get("ui_schema")).get("fields"))
                if isinstance(field, dict)
                and str(field.get("path") or field.get("field_id") or "").strip()
            }
            form_data = _as_dict(value.get("data"))
            # Save submits the complete form state, so every field must be in
            # that action's closed parameter contract. Test/validate controls
            # may intentionally consume only a subset (for example a selected
            # channel), and are validated by their own action declarations.
            for action_key in ("save_action",):
                action_id = str(form_data.get(action_key) or "").strip()
                if not action_id or action_id not in action_ids:
                    continue
                missing_fields = sorted(
                    form_fields - (action_parameters or {}).get(action_id, set())
                )
                if missing_fields:
                    issues.append(
                        _issue(
                            severity="error",
                            code="form_action_parameter_mismatch",
                            message=(
                                f"Form {action_key} '{action_id}' does not declare parameters: "
                                f"{', '.join(missing_fields)}"
                            ),
                            file_path=manifest_path,
                            field_path=f"{base_path}.data.{action_key}",
                        )
                    )
            for action_key in (
                "save_action",
                "validate_action",
                "test_action",
                "load_query",
            ):
                if str(value.get(action_key) or "").strip():
                    issues.append(
                        _issue(
                            severity="error",
                            code="top_level_form_action_not_supported",
                            message=f"Form {action_key} must be declared in data.{action_key}",
                            file_path=manifest_path,
                            field_path=f"{base_path}.{action_key}",
                        )
                    )
        elif component_type == "grid":
            columns = value.get("columns")
            if not isinstance(columns, list):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_grid_columns",
                        message="Grid columns must be a list",
                        file_path=manifest_path,
                        field_path=f"{base_path}.columns",
                    )
                )
            else:
                for col_idx, column_payload in enumerate(columns):
                    if not isinstance(column_payload, dict):
                        issues.append(
                            _issue(
                                severity="error",
                                code="invalid_grid_column",
                                message="Grid column must be an object",
                                file_path=manifest_path,
                                field_path=f"{base_path}.columns[{col_idx}]",
                            )
                        )
                        continue
                    column_type = str(column_payload.get("type") or "").strip().lower()
                    if not column_type:
                        issues.append(
                            _issue(
                                severity="error",
                                code="missing_grid_column_type",
                                message="Grid column type is required",
                                file_path=manifest_path,
                                field_path=f"{base_path}.columns[{col_idx}].type",
                            )
                        )
                    elif column_type not in ALLOWED_GRID_COLUMN_TYPES:
                        issues.append(
                            _issue(
                                severity="error",
                                code="unsupported_grid_column_type",
                                message=f"Unsupported grid column type '{column_type}'",
                                file_path=manifest_path,
                                field_path=f"{base_path}.columns[{col_idx}].type",
                            )
                        )
        elif component_type == "external_resource_picker":
            bootstrap_action_id = str(
                _as_dict(value.get("data")).get("bootstrap_action_id") or ""
            ).strip()
            if bootstrap_action_id in action_ids and bootstrap_action_id not in (
                browser_bootstrap_action_ids or set()
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="unsafe_external_resource_picker_bootstrap_action",
                        message=(
                            "Picker bootstrap actions must use the closed read-only browser "
                            "bootstrap profile"
                        ),
                        file_path=manifest_path,
                        field_path=f"{base_path}.data.bootstrap_action_id",
                    )
                )
            if not _validate_external_resource_picker(
                component=value,
                component_index=component_index or {},
                action_ids=action_ids,
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_external_resource_picker_declaration",
                        message="external_resource_picker must satisfy the closed, bounded V1 host contract",
                        file_path=manifest_path,
                        field_path=f"{base_path}.data.selection",
                    )
                )
        elif component_type not in ALLOWED_COMPONENT_TYPES:
            issues.append(
                _issue(
                    severity="error",
                    code="unsupported_component_type",
                    message=f"Unsupported component type '{component_type}'",
                    file_path=manifest_path,
                    field_path=f"{base_path}.type",
                    hint="Use one of the supported v1 component-kit types.",
                )
            )

    if "required_scopes" in value:
        _validate_scope_list(
            issues=issues,
            scopes_value=value.get("required_scopes"),
            manifest_path=manifest_path,
            field_path=f"{base_path}.required_scopes",
        )

    for key in ("row_actions", "bulk_actions", "toolbar_actions"):
        actions = _as_list(value.get(key))
        for idx, action_payload in enumerate(actions):
            action_obj = _as_dict(action_payload)
            scope_value = str(action_obj.get("scope") or "").strip().lower()
            if scope_value and scope_value not in GRID_ACTION_SCOPES:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_action_scope",
                        message=f"Unsupported action scope '{scope_value}'",
                        file_path=manifest_path,
                        field_path=f"{base_path}.{key}[{idx}].scope",
                        hint="Use one of: row, bulk, page.",
                    )
                )

    for key, nested in value.items():
        child_path = f"{base_path}.{key}"
        if key in QUERY_REFERENCE_KEYS:
            ref_value = str(nested or "").strip()
            if ref_value and ref_value not in query_ids:
                issues.append(
                    _issue(
                        severity="error",
                        code="unknown_query_reference",
                        message=f"Unknown query reference '{ref_value}'",
                        file_path=manifest_path,
                        field_path=child_path,
                        hint="Declare query_id in query_manifest or fix the reference.",
                    )
                )
        if key in ACTION_REFERENCE_KEYS:
            ref_value = str(nested or "").strip()
            if ref_value and ref_value not in action_ids:
                issues.append(
                    _issue(
                        severity="error",
                        code="unknown_action_reference",
                        message=f"Unknown action reference '{ref_value}'",
                        file_path=manifest_path,
                        field_path=child_path,
                        hint="Declare action_id in action_manifest or fix the reference.",
                    )
                )

        _walk_component_tree(
            nested,
            manifest_path=manifest_path,
            base_path=child_path,
            query_ids=query_ids,
            action_ids=action_ids,
            action_parameters=action_parameters,
            issues=issues,
            in_component_slot=(key == "components"),
            component_index=component_index,
            browser_bootstrap_action_ids=browser_bootstrap_action_ids,
        )


__all__ = [
    "_component_index",
    "_resolve_component_block",
    "_resolve_ui_manifest",
    "_validate_scope_shape",
    "_validate_scope_list",
    "_walk_component_tree",
]
