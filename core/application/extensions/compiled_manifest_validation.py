"""Validate the complete compiled extension contract before it becomes executable."""

from __future__ import annotations

import json
import re

from core.application.extensions.identity import normalize_extension_id
from core.application.extensions.oauth_contract import (
    declared_ui_page_ids,
    is_safe_oauth_post_connect_action,
)
from core.application.extensions.remote_runtime_protocol_v2 import HOST_CONTRACT_VERSION
from core.application.extensions.semver import compare_semver, normalize_semver
from core.infrastructure.extension_runtime.extension_browser_picker_contract import (
    _safe_browser_bootstrap_action_ids,
    _validate_external_resource_picker,
)
from core.infrastructure.extension_runtime.extension_runtime_common import (
    ACTION_ID_REQUIRED_MESSAGE,
    ALLOWED_OPERATION_HANDLER_MODES,
    COMPILED_MANIFEST_ALLOWED_FIELDS,
    QUERY_ID_REQUIRED_MESSAGE,
    UUID,
)
from core.infrastructure.extension_runtime.extension_runtime_feature_validation import (
    _validate_feature_profiles,
    _validate_task_ingress_profile,
)
from core.infrastructure.extension_runtime.extension_runtime_manifest import (
    _normalize_command,
    _normalize_healthcheck,
)
from core.infrastructure.extension_runtime.extension_runtime_ui_manifest import (
    _component_index,
    _validate_scope_list,
    _walk_component_tree,
)
from core.infrastructure.extension_runtime.extension_runtime_validation import (
    _as_dict,
    _as_list,
    _issue,
    _normalize_string_list,
    _split_issues,
    _validate_callback_definitions,
    _validate_event_subscription_list,
    _validate_workflow_trigger_definitions,
)
from core.infrastructure.extension_runtime.python_dependencies import validate_bundled_wheels
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
)

from .compiled_manifest_policies import (
    _validate_action_runtime_contract,
    _validate_external_effect_pre_save_actions,
    _validate_external_effects,
    _validate_notification_provider_delivery_contract,
    _validate_project_state_declaration,
    _validate_runtime_grants,
)


def validate_compiled_manifest(compiled_manifest: dict) -> dict:
    issues: list[dict] = []
    manifest_version = int(compiled_manifest.get("manifest_version") or 0)
    manifest_path = str(compiled_manifest.get("manifest_path") or "")
    kind = str(compiled_manifest.get("kind") or "").strip().lower()
    features = _normalize_string_list(compiled_manifest.get("features"))
    extension_id = str(compiled_manifest.get("extension_id") or "").strip()

    lifecycle = _as_dict(compiled_manifest.get("lifecycle"))
    try:
        data_schema_version = int(lifecycle.get("data_schema_version") or 1)
        from core.application.extensions.data_migration_plan import plan_data_migration_steps

        if data_schema_version > 1:
            plan_data_migration_steps(
                lifecycle,
                source_version=1,
                target_version=data_schema_version,
            )
        elif lifecycle.get("data_migrations"):
            raise ValueError("Data schema version 1 must not declare migrations")
        for namespace, schema in _as_dict(lifecycle.get("project_state_schemas")).items():
            if not str(namespace or "").strip() or not isinstance(schema, dict):
                raise ValueError("Project-state migration schemas require object values")
            encoded_schema = json.dumps(schema, sort_keys=True)
            if (
                len(encoded_schema.encode("utf-8")) > 262_144
                or '"$ref": "http' in encoded_schema.lower()
            ):
                raise ValueError("Project-state migration schemas must be bounded and bundle-local")
    except (TypeError, ValueError) as exc:
        issues.append(
            _issue(
                severity="error",
                code="invalid_data_migration_chain",
                message=str(exc),
                file_path=manifest_path,
                field_path="lifecycle",
            )
        )

    for field_name in sorted(set(compiled_manifest) - COMPILED_MANIFEST_ALLOWED_FIELDS):
        issues.append(
            _issue(
                severity="error",
                code="unknown_compiled_manifest_field",
                message=f"Compiled manifest field '{field_name}' is not supported.",
                file_path=manifest_path,
                field_path=field_name,
                hint="Remove unsupported top-level fields from the compiled manifest.",
            )
        )

    _validate_scope_list(
        issues=issues,
        scopes_value=compiled_manifest.get("required_scopes"),
        manifest_path=manifest_path,
        field_path="required_scopes",
    )
    compiled_manifest["runtime_grants"] = _validate_runtime_grants(
        compiled_manifest.get("runtime_grants"),
        extension_id=extension_id,
        issues=issues,
        manifest_path=manifest_path,
    )
    _validate_extension_dependencies(
        issues=issues,
        manifest_path=manifest_path,
        extension_id=extension_id,
        dependencies=_as_dict(compiled_manifest.get("dependencies")),
    )
    python_requirement_wheel_issues = validate_bundled_wheels(
        bundle_root=str(compiled_manifest.get("bundle_root") or ""),
        requirements=_as_list(compiled_manifest.get(PYTHON_REQUIREMENTS_FIELD)),
        manifest_path=manifest_path,
    )
    issues.extend(python_requirement_wheel_issues)

    if manifest_version != 2:
        issues.append(
            _issue(
                severity="error",
                code="manifest_v2_required",
                message="Canonical extension packages require manifest_version: 2.",
                file_path=manifest_path,
                field_path="manifest_version",
            )
        )

    if not features:
        issues.append(
            _issue(
                severity="error",
                code="v2_missing_features",
                message="Manifest v2 requires features",
                file_path=manifest_path,
                field_path="features",
            )
        )

    if manifest_version == 2:
        healthcheck = _normalize_healthcheck(compiled_manifest.get("healthcheck"))
        if not _normalize_command(healthcheck.get("command")):
            issues.append(
                _issue(
                    severity="error",
                    code="v2_missing_health",
                    message="Manifest v2 requires health.command",
                    file_path=manifest_path,
                    field_path="health.command",
                    hint='Add health: {command: ["python3", "health.py"], timeout_seconds: 5}.',
                )
            )
        ui_manifest_ref = str(compiled_manifest.get("ui_manifest_ref") or "").strip()
        if not ui_manifest_ref:
            issues.append(
                _issue(
                    severity="error",
                    code="v2_missing_ui_manifest",
                    message="Manifest v2 requires ui_manifest ref",
                    file_path=manifest_path,
                    field_path="ui_manifest",
                )
            )

    machine_auth = _as_dict(compiled_manifest.get("machine_auth"))
    for key in ("required_scopes", "scopes"):
        if key in machine_auth:
            _validate_scope_list(
                issues=issues,
                scopes_value=machine_auth.get(key),
                manifest_path=manifest_path,
                field_path=f"machine_auth.{key}",
            )

    account_scope = _as_dict(compiled_manifest.get("account_scope"))
    _validate_event_subscription_list(
        issues=issues,
        subscriptions_value=compiled_manifest.get("event_subscriptions"),
        manifest_path=manifest_path,
        field_path="event_subscriptions",
    )
    if "subscriptions" in account_scope:
        _validate_event_subscription_list(
            issues=issues,
            subscriptions_value=account_scope.get("subscriptions"),
            manifest_path=manifest_path,
            field_path="account_scope.subscriptions",
        )
    if "event_subscriptions" in account_scope:
        _validate_event_subscription_list(
            issues=issues,
            subscriptions_value=account_scope.get("event_subscriptions"),
            manifest_path=manifest_path,
            field_path="account_scope.event_subscriptions",
        )

    if "account_id" in account_scope and not str(account_scope.get("account_id") or "").strip():
        issues.append(
            _issue(
                severity="error",
                code="invalid_account_scope",
                message="account_scope.account_id must be a non-empty string",
                file_path=manifest_path,
                field_path="account_scope.account_id",
            )
        )
    if "allowed_project_ids" in account_scope:
        raw_ids = account_scope.get("allowed_project_ids")
        if not isinstance(raw_ids, list):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_account_scope_projects",
                    message="account_scope.allowed_project_ids must be a list of non-empty UUID strings",
                    file_path=manifest_path,
                    field_path="account_scope.allowed_project_ids",
                )
            )
        else:
            for idx, value in enumerate(raw_ids):
                normalized = str(value or "").strip()
                invalid_project_id = not normalized
                if not invalid_project_id:
                    try:
                        UUID(normalized)
                    except ValueError:
                        invalid_project_id = True
                if invalid_project_id:
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_account_scope_projects",
                            message=f"Invalid project id '{value}'",
                            file_path=manifest_path,
                            field_path=f"account_scope.allowed_project_ids[{idx}]",
                        )
                    )
    if "allowed_workflow_ids" in account_scope:
        raw_workflow_ids = account_scope.get("allowed_workflow_ids")
        if not isinstance(raw_workflow_ids, list):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_account_scope_workflows",
                    message="account_scope.allowed_workflow_ids must be a list of non-empty strings",
                    file_path=manifest_path,
                    field_path="account_scope.allowed_workflow_ids",
                )
            )
        else:
            for idx, value in enumerate(raw_workflow_ids):
                if not str(value or "").strip():
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_account_scope_workflows",
                            message="Workflow id must be a non-empty string",
                            file_path=manifest_path,
                            field_path=f"account_scope.allowed_workflow_ids[{idx}]",
                        )
                    )

    query_definitions = _as_list(compiled_manifest.get("query_definitions"))
    query_ids = set()
    for idx, row in enumerate(query_definitions):
        query_payload = _as_dict(row)
        qid = str(query_payload.get("query_id") or "").strip()
        if not qid:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_query_id",
                    message=QUERY_ID_REQUIRED_MESSAGE,
                    file_path=manifest_path,
                    field_path=f"query_definitions[{idx}].query_id",
                )
            )
            continue
        if qid in query_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_query_id",
                    message=f"Duplicate query_id '{qid}'",
                    file_path=manifest_path,
                    field_path=f"query_definitions[{idx}].query_id",
                )
            )
            continue
        query_ids.add(qid)
        _validate_scope_list(
            issues=issues,
            scopes_value=query_payload.get("required_scopes"),
            manifest_path=manifest_path,
            field_path=f"query_definitions[{idx}].required_scopes",
        )
        query_handler = _as_dict(query_payload.get("handler"))
        query_handler_mode = (
            str(query_handler.get("mode") or "extension").strip().lower() or "extension"
        )
        if query_handler_mode == "core":
            query_handler_mode = "platform"
        if query_handler_mode not in ALLOWED_OPERATION_HANDLER_MODES:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_operation_handler_mode",
                    message=f"Unsupported query handler.mode '{query_handler_mode}'",
                    file_path=manifest_path,
                    field_path=f"query_definitions[{idx}].handler.mode",
                    hint="Use handler.mode = extension or platform.",
                )
            )

    action_definitions = _as_list(compiled_manifest.get("action_definitions"))
    action_ids = set()
    for idx, row in enumerate(action_definitions):
        action_payload = _as_dict(row)
        aid = str(action_payload.get("action_id") or "").strip()
        if not aid:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_action_id",
                    message=ACTION_ID_REQUIRED_MESSAGE,
                    file_path=manifest_path,
                    field_path=f"action_definitions[{idx}].action_id",
                )
            )
            continue
        if aid in action_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_action_id",
                    message=f"Duplicate action_id '{aid}'",
                    file_path=manifest_path,
                    field_path=f"action_definitions[{idx}].action_id",
                )
            )
            continue
        action_ids.add(aid)
        _validate_scope_list(
            issues=issues,
            scopes_value=action_payload.get("required_scopes"),
            manifest_path=manifest_path,
            field_path=f"action_definitions[{idx}].required_scopes",
        )
        action_handler = _as_dict(action_payload.get("handler"))
        action_handler_mode = (
            str(action_handler.get("mode") or "extension").strip().lower() or "extension"
        )
        if action_handler_mode == "core":
            action_handler_mode = "platform"
        if action_handler_mode not in ALLOWED_OPERATION_HANDLER_MODES:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_operation_handler_mode",
                    message=f"Unsupported action handler.mode '{action_handler_mode}'",
                    file_path=manifest_path,
                    field_path=f"action_definitions[{idx}].handler.mode",
                    hint="Use handler.mode = extension or platform.",
                )
            )

        action_handler_config = _as_dict(action_handler.get("config"))
        _validate_action_runtime_contract(
            action_payload.get("contract"),
            issues=issues,
            manifest_path=manifest_path,
            field_path=f"action_definitions[{idx}].contract",
        )
        pre_dispatch_host_mutations = action_handler_config.get("pre_dispatch_host_mutations")
        if pre_dispatch_host_mutations is not None:
            field_path = f"action_definitions[{idx}].handler.config.pre_dispatch_host_mutations"
            if (
                not isinstance(pre_dispatch_host_mutations, list)
                or not pre_dispatch_host_mutations
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in pre_dispatch_host_mutations
                )
                or len({item.strip() for item in pre_dispatch_host_mutations})
                != len(pre_dispatch_host_mutations)
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_pre_dispatch_host_mutations",
                        message=(
                            "handler.config.pre_dispatch_host_mutations must be a "
                            "non-empty list of unique mutation kinds"
                        ),
                        file_path=manifest_path,
                        field_path=field_path,
                    )
                )
            else:
                declared_host_mutations = {
                    str(item or "").strip()
                    for item in _as_list(action_handler_config.get("host_mutations"))
                    if str(item or "").strip()
                }
                normalized_pre_dispatch = [item.strip() for item in pre_dispatch_host_mutations]
                if any(item not in declared_host_mutations for item in normalized_pre_dispatch):
                    issues.append(
                        _issue(
                            severity="error",
                            code="pre_dispatch_host_mutation_undeclared",
                            message=(
                                "Every pre-dispatch host mutation must also be "
                                "declared in handler.config.host_mutations"
                            ),
                            file_path=manifest_path,
                            field_path=field_path,
                        )
                    )
                if not bool(action_payload.get("mutates_platform")):
                    issues.append(
                        _issue(
                            severity="error",
                            code="pre_dispatch_platform_mutation_required",
                            message=(
                                "Actions with pre-dispatch host mutations must set "
                                "mutates_platform=true"
                            ),
                            file_path=manifest_path,
                            field_path=f"action_definitions[{idx}].mutates_platform",
                        )
                    )

        if (
            bool(action_payload.get("mutates_platform"))
            and not str(action_payload.get("command_type") or "").strip()
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="missing_command_type",
                    message=(
                        "Action marked mutates_platform=true must declare command_type "
                        "to route through command bus"
                    ),
                    file_path=manifest_path,
                    field_path=f"action_definitions[{idx}].command_type",
                )
            )

    compiled_manifest["callbacks"] = _validate_callback_definitions(
        compiled_manifest.get("callbacks"),
        issues=issues,
        manifest_path=manifest_path,
        action_ids=action_ids,
    )
    compiled_manifest["workflow_triggers"] = _validate_workflow_trigger_definitions(
        compiled_manifest.get("workflow_triggers"),
        issues=issues,
        manifest_path=manifest_path,
    )
    compiled_manifest["external_effects"] = _validate_external_effects(
        compiled_manifest.get("external_effects"),
        issues=issues,
        manifest_path=manifest_path,
        action_ids=action_ids,
        query_ids=query_ids,
        manifest_kind=str(compiled_manifest.get("kind") or "").strip().lower(),
    )
    _validate_external_effect_pre_save_actions(
        compiled_manifest,
        issues=issues,
        manifest_path=manifest_path,
    )
    _validate_notification_provider_delivery_contract(
        compiled_manifest,
        issues=issues,
        manifest_path=manifest_path,
    )
    compiled_manifest["project_state"] = _validate_project_state_declaration(
        compiled_manifest.get("project_state"),
        issues=issues,
        manifest_path=manifest_path,
    )

    _validate_task_ingress_profile(
        issues=issues,
        manifest_path=manifest_path,
        manifest_version=manifest_version,
        kind=kind,
        features=features,
        query_definitions=[_as_dict(row) for row in query_definitions],
        action_definitions=[_as_dict(row) for row in action_definitions],
    )
    _validate_feature_profiles(
        issues=issues,
        manifest_path=manifest_path,
        kind=kind,
        features=features,
        supported_events=_normalize_string_list(compiled_manifest.get("supported_events")),
        action_definitions=[_as_dict(row) for row in action_definitions],
    )

    ui_manifest = _as_dict(compiled_manifest.get("ui_manifest"))
    pages = _as_list(ui_manifest.get("pages"))
    ui_page_ids = declared_ui_page_ids(compiled_manifest)
    ui_page_anchors: dict[str, set[str]] = {}
    for page_idx, page_payload in enumerate(pages):
        page = _as_dict(page_payload)
        page_id = str(page.get("page_id") or "").strip()
        anchors: set[str] = set()
        for component_idx, component_payload in enumerate(_as_list(page.get("components"))):
            component = _as_dict(component_payload)
            anchor_value = component.get("anchor_id")
            if anchor_value is None:
                continue
            anchor = anchor_value if isinstance(anchor_value, str) else ""
            if (
                not anchor
                or len(anchor) > 64
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", anchor) is None
                or anchor in anchors
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_ui_help_anchor",
                        message="UI anchor_id must be a unique safe lowercase slug on its page",
                        file_path=manifest_path,
                        field_path=(f"ui.pages[{page_idx}].components[{component_idx}].anchor_id"),
                    )
                )
                continue
            anchors.add(anchor)
        ui_page_anchors[page_id] = anchors
    oauth_action_ids = {
        str(row.get("action_id") or "").strip()
        for row in _as_list(compiled_manifest.get("action_definitions"))
        if isinstance(row, dict) and str(row.get("action_id") or "").strip()
    }
    for provider_idx, provider_payload in enumerate(
        _as_list(compiled_manifest.get("oauth_providers"))
    ):
        provider = _as_dict(provider_payload)
        for page_key in ("setup_page_id", "help_page_id"):
            page_id = str(provider.get(page_key) or "").strip()
            if page_id not in ui_page_ids:
                issues.append(
                    _issue(
                        severity="error",
                        code="unknown_oauth_ui_page",
                        message=f"OAuth UI page '{page_id}' is not declared by this extension",
                        file_path=manifest_path,
                        field_path=f"oauth_providers[{provider_idx}].{page_key}",
                    )
                )
        help_page_id = str(provider.get("help_page_id") or "").strip()
        for error_code, anchor_value in _as_dict(provider.get("error_help_anchors")).items():
            anchor = str(anchor_value or "").strip()
            if anchor not in ui_page_anchors.get(help_page_id, set()):
                issues.append(
                    _issue(
                        severity="error",
                        code="unknown_oauth_help_anchor",
                        message=(
                            f"OAuth help anchor '{anchor}' is not declared on page '{help_page_id}'"
                        ),
                        file_path=manifest_path,
                        field_path=(
                            f"oauth_providers[{provider_idx}].error_help_anchors.{error_code}"
                        ),
                    )
                )
        post_connect_action_id = str(provider.get("post_connect_action_id") or "").strip()
        if post_connect_action_id and post_connect_action_id not in oauth_action_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="unknown_oauth_post_connect_action",
                    message=f"Post-connect action '{post_connect_action_id}' is not declared",
                    file_path=manifest_path,
                    field_path=f"oauth_providers[{provider_idx}].post_connect_action_id",
                )
            )
        elif post_connect_action_id and not is_safe_oauth_post_connect_action(
            compiled_manifest, post_connect_action_id
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="unsafe_oauth_post_connect_action",
                    message="Post-connect actions must use the closed automatic action profile",
                    file_path=manifest_path,
                    field_path=f"oauth_providers[{provider_idx}].post_connect_action_id",
                )
            )
    browser_bootstrap_action_ids = _safe_browser_bootstrap_action_ids(compiled_manifest)
    action_parameters = {
        str(row.get("action_id") or "").strip(): {
            str(parameter.get("name") or "").strip()
            for parameter in _as_list(_as_dict(row).get("parameters"))
            if isinstance(parameter, dict) and str(parameter.get("name") or "").strip()
        }
        for row in _as_list(compiled_manifest.get("action_definitions"))
        if isinstance(row, dict) and str(row.get("action_id") or "").strip()
    }
    for idx, page_payload in enumerate(pages):
        page = _as_dict(page_payload)
        components = page.get("components")
        if not isinstance(components, list):
            continue
        _walk_component_tree(
            components,
            manifest_path=manifest_path,
            base_path=f"ui.pages[{idx}].components",
            query_ids=query_ids,
            action_ids=action_ids,
            action_parameters=action_parameters,
            issues=issues,
            in_component_slot=True,
            component_index=_component_index(components),
            browser_bootstrap_action_ids=browser_bootstrap_action_ids,
        )

    for step_idx, step in enumerate(_as_list(compiled_manifest.get("step_ui_manifest"))):
        for field_idx, field in enumerate(_as_list(_as_dict(step).get("fields"))):
            field_payload = _as_dict(field)
            declaration = field_payload.get("external_resource_picker")
            if declaration is None:
                continue
            field_name = str(field_payload.get("name") or "").strip()
            target_form = {
                "component_id": "workflow_parameter_form",
                "type": "form",
                "ui_schema": {"fields": [{"field_id": field_name}]},
            }
            picker_component = {
                "component_id": f"workflow_picker_{step_idx}_{field_idx}",
                "type": "external_resource_picker",
                "data": declaration,
            }
            bootstrap_action_id = str(
                _as_dict(declaration).get("bootstrap_action_id") or ""
            ).strip()
            field_path = (
                f"step_ui_manifest[{step_idx}].fields[{field_idx}].external_resource_picker"
            )
            if bootstrap_action_id not in browser_bootstrap_action_ids:
                issues.append(
                    _issue(
                        severity="error",
                        code="unsafe_external_resource_picker_bootstrap_action",
                        message=(
                            "Picker bootstrap actions must use the closed read-only browser "
                            "bootstrap profile"
                        ),
                        file_path=manifest_path,
                        field_path=f"{field_path}.bootstrap_action_id",
                    )
                )
            if not _validate_external_resource_picker(
                component=picker_component,
                component_index={"workflow_parameter_form": target_form},
                action_ids=action_ids,
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_external_resource_picker_declaration",
                        message=(
                            "external_resource_picker must satisfy the closed, bounded V1 "
                            "host contract"
                        ),
                        file_path=manifest_path,
                        field_path=field_path,
                    )
                )

    errors, warnings = _split_issues(issues)
    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
    }


def host_contract_errors(compiled_manifest: dict) -> list[dict]:
    """Errors for a release that does not declare the host contract this Core speaks."""
    # The host invokes only releases built for its own contract (no fallback), so
    # a release that names none or another one would install, show healthy and
    # then fail every action; refuse it at validation instead.
    declared = str(_as_dict(compiled_manifest.get("runtime")).get("contract_version") or "").strip()
    if declared == HOST_CONTRACT_VERSION:
        return []
    if declared:
        code = "unsupported_host_contract"
        message = (
            f"runtime.contract_version is '{declared}', but this Flow Steward only invokes "
            f"releases built for host contract '{HOST_CONTRACT_VERSION}'."
        )
    else:
        code = "missing_host_contract"
        message = (
            "runtime.contract_version is missing; this Flow Steward only invokes releases "
            f"built for host contract '{HOST_CONTRACT_VERSION}'."
        )
    return [
        _issue(
            severity="error",
            code=code,
            message=message,
            file_path=str(compiled_manifest.get("manifest_path") or ""),
            field_path="runtime.contract_version",
            hint=(
                f"Declare runtime.contract_version: {HOST_CONTRACT_VERSION} once the extension "
                "speaks that contract (no phase_id in envelopes or grants) and cut a new release."
            ),
        )
    ]


def _validate_extension_dependencies(
    *,
    issues: list[dict],
    manifest_path: str,
    extension_id: str,
    dependencies: dict,
) -> None:
    raw_extensions = dependencies.get("extensions")
    if raw_extensions in (None, ""):
        return
    if not isinstance(raw_extensions, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_extension_dependencies",
                message="dependencies.extensions must be a list",
                file_path=manifest_path,
                field_path="dependencies.extensions",
            )
        )
        return
    seen: set[str] = set()
    for idx, raw_item in enumerate(raw_extensions):
        item = _as_dict(raw_item)
        dep_id = str(item.get("extension_id") or "").strip()
        field_path = f"dependencies.extensions[{idx}].extension_id"
        try:
            dep_id = normalize_extension_id(dep_id, field_path)
        except ValueError as exc:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_extension_dependency_id",
                    message=str(exc),
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
            continue
        if dep_id == extension_id:
            issues.append(
                _issue(
                    severity="error",
                    code="extension_dependency_self_reference",
                    message=f"Extension '{extension_id}' cannot depend on itself",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
        if dep_id in seen:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_extension_dependency",
                    message=f"Duplicate extension dependency '{dep_id}'",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
        seen.add(dep_id)
        for version_key in ("min_version", "max_version_exclusive"):
            version_value = item.get(version_key)
            if version_value in (None, ""):
                continue
            try:
                normalize_semver(version_value, f"dependencies.extensions[{idx}].{version_key}")
            except ValueError as exc:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_extension_dependency_version",
                        message=str(exc),
                        file_path=manifest_path,
                        field_path=f"dependencies.extensions[{idx}].{version_key}",
                    )
                )
        min_version = item.get("min_version")
        max_version = item.get("max_version_exclusive")
        if min_version not in (None, "") and max_version not in (None, ""):
            try:
                if (
                    compare_semver(
                        min_version,
                        max_version,
                        field_name=f"dependencies.extensions[{idx}] version range",
                    )
                    >= 0
                ):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_extension_dependency_version_range",
                            message="dependency min_version must be < max_version_exclusive",
                            file_path=manifest_path,
                            field_path=f"dependencies.extensions[{idx}]",
                        )
                    )
            except ValueError:
                pass
