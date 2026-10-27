# ruff: noqa: F403, F405, RUF022
from __future__ import annotations

from .extension_runtime_common import *  # NOSONAR


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _normalize_string_list(value: Any) -> list[str]:
    normalized: list[str] = []
    for item in value or []:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return list(dict.fromkeys(normalized))


def _normalize_subscription_severity(value: Any) -> str:
    normalized = str(value or "any").strip().lower() or "any"
    if normalized == "warn":
        normalized = "warning"
    return normalized if normalized in ALLOWED_SUBSCRIPTION_SEVERITIES else "any"


def _normalize_event_subscriptions(value: Any) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    if isinstance(value, (list, tuple, set)):
        values = value
    elif value is not None:
        values = [value]
    else:
        values = []
    for raw in values:
        selector: dict[str, Any]
        if isinstance(raw, str):
            event_type = str(raw or "").strip()
            if not event_type:
                continue
            selector = {"event_type": event_type}
        elif isinstance(raw, dict):
            selector = dict(raw)
        else:
            continue

        event_type = str(
            selector.get("event_type")
            or selector.get("event")
            or selector.get("event_pattern")
            or ""
        ).strip()
        if not event_type:
            continue

        payload: dict[str, Any] = {
            "event_type": event_type,
            "severity": _normalize_subscription_severity(selector.get("severity")),
            "conditions": _as_dict(selector.get("conditions")),
        }
        project_id_raw = selector.get("project_id")
        if project_id_raw is not None:
            project_id = str(project_id_raw or "").strip()
            if project_id:
                payload["project_id"] = project_id
        if selector.get("workflow_id") is not None:
            workflow_id = str(selector.get("workflow_id") or "").strip()
            if workflow_id:
                payload["workflow_id"] = workflow_id

        dedupe_key = json.dumps(payload, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(payload)
    return normalized


def _callback_definitions_value(value: Any) -> Any:
    if value is None:
        return []
    return value


def _workflow_trigger_definitions_value(value: Any) -> Any:
    if value is None:
        return []
    return value


def _validate_workflow_trigger_definitions(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_workflow_triggers",
                message="workflow_triggers must be a list",
                file_path=manifest_path,
                field_path="workflow_triggers",
            )
        )
        return []

    normalized: list[dict] = []
    seen_catalog_ids: set[str] = set()
    for idx, raw_item in enumerate(value):
        field_path = f"workflow_triggers[{idx}]"
        if not isinstance(raw_item, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_workflow_trigger",
                    message="Workflow trigger declaration must be an object",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
            continue
        item = dict(raw_item)
        trigger_type_id = str(item.get("trigger_type_id") or item.get("id") or "").strip()
        provider_key = str(item.get("provider_key") or "").strip().lower()
        display_name = str(item.get("display_name") or item.get("title") or "").strip()
        config_schema = _as_dict(item.get("config_schema"))
        ui_schema = _as_dict(item.get("ui_schema"))
        if not trigger_type_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_workflow_trigger_type_id",
                    message="workflow_triggers[].trigger_type_id is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.trigger_type_id",
                )
            )
        if not provider_key:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_workflow_trigger_provider_key",
                    message="workflow_triggers[].provider_key is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.provider_key",
                )
            )
        if not display_name:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_workflow_trigger_display_name",
                    message="workflow_triggers[].display_name is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.display_name",
                )
            )
        if not config_schema:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_workflow_trigger_config_schema",
                    message="workflow_triggers[].config_schema is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.config_schema",
                )
            )
        if not ui_schema:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_workflow_trigger_ui_schema",
                    message="workflow_triggers[].ui_schema is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.ui_schema",
                )
            )
        dedupe_key = f"{provider_key}:{trigger_type_id}"
        if provider_key and trigger_type_id and dedupe_key in seen_catalog_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_workflow_trigger_definition",
                    message=f"Duplicate workflow trigger '{dedupe_key}'",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
        else:
            seen_catalog_ids.add(dedupe_key)
        normalized.append(
            {
                "trigger_type_id": trigger_type_id,
                "provider_key": provider_key,
                "display_name": display_name,
                "description": str(item.get("description") or "").strip(),
                "config_schema": config_schema,
                "ui_schema": ui_schema,
                "event_name": str(item.get("event_name") or item.get("event") or "").strip(),
                "event_metadata": _as_dict(item.get("event_metadata")),
                "provider_ingress_url_required": bool(item.get("provider_ingress_url_required")),
            }
        )
    return normalized


def _validate_callback_definitions(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
    action_ids: set[str] | None = None,
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_callbacks",
                message="callbacks must be a list",
                file_path=manifest_path,
                field_path="callbacks",
            )
        )
        return []

    declared_action_ids = set(action_ids or set())
    validate_action_refs = action_ids is not None
    normalized: list[dict] = []
    seen_callback_ids: set[str] = set()

    for idx, raw_callback in enumerate(value):
        field_path = f"callbacks[{idx}]"
        if not isinstance(raw_callback, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_callback",
                    message="Callback declaration must be an object",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
            continue

        callback = dict(raw_callback)
        callback_id = str(callback.get("callback_id") or "").strip()
        purpose = str(callback.get("purpose") or "").strip()
        action_id = str(callback.get("action_id") or "").strip()
        oauth_authorization = purpose == "oauth_authorization"

        if not callback_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_callback_id",
                    message="callback_id is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.callback_id",
                )
            )
        elif not CALLBACK_ID_RE.match(callback_id):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_callback_id",
                    message="callback_id must be a stable slug",
                    file_path=manifest_path,
                    field_path=f"{field_path}.callback_id",
                    hint="Use lowercase letters, numbers, hyphen, or underscore.",
                )
            )
        elif callback_id in seen_callback_ids:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_callback_id",
                    message=f"Duplicate callback_id '{callback_id}'",
                    file_path=manifest_path,
                    field_path=f"{field_path}.callback_id",
                )
            )
        else:
            seen_callback_ids.add(callback_id)

        if not purpose:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_callback_purpose",
                    message="callback purpose is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.purpose",
                )
            )

        methods_value = callback.get("methods")
        methods: list[str] = []
        if not isinstance(methods_value, list) or not methods_value:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_callback_methods",
                    message="callback methods must be a non-empty list",
                    file_path=manifest_path,
                    field_path=f"{field_path}.methods",
                )
            )
        else:
            for method_idx, raw_method in enumerate(methods_value):
                method = str(raw_method or "").strip().upper()
                if not method or method not in ALLOWED_CALLBACK_METHODS:
                    issues.append(
                        _issue(
                            severity="error",
                            code="unsupported_callback_method",
                            message=f"Unsupported callback method '{raw_method}'",
                            file_path=manifest_path,
                            field_path=f"{field_path}.methods[{method_idx}]",
                            hint="MVP callback methods are GET and POST.",
                        )
                    )
                    continue
                if method not in methods:
                    methods.append(method)

        if oauth_authorization and methods != ["GET"]:
            issues.append(
                _issue(
                    severity="error",
                    code="unsupported_oauth_callback_method",
                    message="OAuth authorization callbacks require exactly methods: [GET]",
                    file_path=manifest_path,
                    field_path=f"{field_path}.methods",
                )
            )

        if oauth_authorization and action_id:
            issues.append(
                _issue(
                    severity="error",
                    code="oauth_callback_action_forbidden",
                    message="OAuth authorization callbacks are dispatched by the platform",
                    file_path=manifest_path,
                    field_path=f"{field_path}.action_id",
                )
            )
        elif not oauth_authorization and not action_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_callback_action_id",
                    message="callback action_id is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.action_id",
                )
            )
        elif (
            not oauth_authorization
            and validate_action_refs
            and action_id not in declared_action_ids
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="unknown_callback_action",
                    message=f"Callback action_id '{action_id}' is not declared in action_manifest",
                    file_path=manifest_path,
                    field_path=f"{field_path}.action_id",
                )
            )

        connection_scoped = callback.get("connection_scoped", False)
        if "connection_scoped" in callback and not isinstance(connection_scoped, bool):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_callback_connection_scoped",
                    message="callback connection_scoped must be a boolean",
                    file_path=manifest_path,
                    field_path=f"{field_path}.connection_scoped",
                )
            )
            connection_scoped = False
        if oauth_authorization and "connection_scoped" not in callback:
            issues.append(
                _issue(
                    severity="error",
                    code="oauth_callback_must_be_project_scoped",
                    message="OAuth authorization callbacks must declare connection_scoped=false",
                    file_path=manifest_path,
                    field_path=f"{field_path}.connection_scoped",
                )
            )
        if oauth_authorization and connection_scoped:
            issues.append(
                _issue(
                    severity="error",
                    code="oauth_callback_must_be_project_scoped",
                    message="OAuth authorization callbacks must be project scoped",
                    file_path=manifest_path,
                    field_path=f"{field_path}.connection_scoped",
                )
            )

        auth_value = callback.get("auth") or {"type": "none"}
        if not isinstance(auth_value, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_callback_auth",
                    message="callback auth must be an object",
                    file_path=manifest_path,
                    field_path=f"{field_path}.auth",
                )
            )
            auth_value = {"type": "none"}

        auth = dict(auth_value)
        auth_type = str(auth.get("type") or "none").strip().lower() or "none"
        if oauth_authorization and auth_type != "none":
            issues.append(
                _issue(
                    severity="error",
                    code="oauth_callback_auth_forbidden",
                    message="OAuth authorization callbacks use platform state validation",
                    file_path=manifest_path,
                    field_path=f"{field_path}.auth",
                )
            )
        if auth_type not in ALLOWED_CALLBACK_AUTH_TYPES:
            issues.append(
                _issue(
                    severity="error",
                    code="unsupported_callback_auth_type",
                    message=f"Unsupported callback auth.type '{auth_type}'",
                    file_path=manifest_path,
                    field_path=f"{field_path}.auth.type",
                    hint="Supported callback auth types are none, shared_secret_header, and hmac_sha256.",
                )
            )

        normalized_auth: dict[str, Any] = {"type": auth_type}
        if auth_type == "shared_secret_header":
            header = str(auth.get("header") or "").strip()
            if not header:
                issues.append(
                    _issue(
                        severity="error",
                        code="missing_callback_auth_header",
                        message="shared_secret_header callback auth requires auth.header",
                        file_path=manifest_path,
                        field_path=f"{field_path}.auth.header",
                    )
                )
            else:
                normalized_auth["header"] = header
        elif auth_type == "hmac_sha256":
            for key in ("signature_header", "timestamp_header", "delivery_id_header"):
                header = str(auth.get(key) or "").strip()
                if not header:
                    issues.append(
                        _issue(
                            severity="error",
                            code="missing_callback_auth_header",
                            message=f"hmac_sha256 callback auth requires auth.{key}",
                            file_path=manifest_path,
                            field_path=f"{field_path}.auth.{key}",
                        )
                    )
                else:
                    normalized_auth[key] = header
            try:
                max_age_seconds = int(auth.get("max_age_seconds") or 300)
            except (TypeError, ValueError):
                max_age_seconds = 0
            if not 30 <= max_age_seconds <= 3600:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_callback_auth_max_age",
                        message="hmac_sha256 auth.max_age_seconds must be between 30 and 3600",
                        file_path=manifest_path,
                        field_path=f"{field_path}.auth.max_age_seconds",
                    )
                )
            else:
                normalized_auth["max_age_seconds"] = max_age_seconds

        normalized_callback = {
            "callback_id": callback_id,
            "purpose": purpose,
            "methods": methods,
            "connection_scoped": bool(connection_scoped),
            "auth": normalized_auth,
        }
        if not oauth_authorization:
            normalized_callback["action_id"] = action_id
        normalized.append(normalized_callback)

    return normalized


def _validate_event_subscription_list(
    *,
    issues: list[dict],
    subscriptions_value: Any,
    manifest_path: str,
    field_path: str,
) -> None:
    if subscriptions_value is None:
        return
    if not isinstance(subscriptions_value, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_event_subscriptions",
                message=f"{field_path} must be a list",
                file_path=manifest_path,
                field_path=field_path,
            )
        )
        return
    for idx, item in enumerate(subscriptions_value):
        current_path = f"{field_path}[{idx}]"
        if isinstance(item, str):
            if not str(item or "").strip():
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_event_subscription_selector",
                        message="Subscription event selector string must be non-empty",
                        file_path=manifest_path,
                        field_path=current_path,
                    )
                )
            continue
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_event_subscription_selector",
                    message="Subscription selector must be an object or event type string",
                    file_path=manifest_path,
                    field_path=current_path,
                )
            )
            continue
        event_type = str(
            item.get("event_type") or item.get("event") or item.get("event_pattern") or ""
        ).strip()
        if not event_type:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_event_subscription_event_type",
                    message="Subscription selector must declare event_type",
                    file_path=manifest_path,
                    field_path=f"{current_path}.event_type",
                )
            )
        severity = str(item.get("severity") or "any").strip().lower() or "any"
        if severity == "warn":
            severity = "warning"
        if severity not in ALLOWED_SUBSCRIPTION_SEVERITIES:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_event_subscription_severity",
                    message=f"Unsupported subscription severity '{severity}'",
                    file_path=manifest_path,
                    field_path=f"{current_path}.severity",
                    hint="Use one of: any, debug, info, warning, error, critical.",
                )
            )
        if "conditions" in item and not isinstance(item.get("conditions"), dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_event_subscription_conditions",
                    message="Subscription conditions must be an object",
                    file_path=manifest_path,
                    field_path=f"{current_path}.conditions",
                )
            )
        project_id_raw = item.get("project_id")
        if project_id_raw is not None:
            normalized_project_id = str(project_id_raw or "").strip()
            invalid_project_id = not normalized_project_id
            if not invalid_project_id:
                try:
                    UUID(normalized_project_id)
                except ValueError:
                    invalid_project_id = True
            if invalid_project_id:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_event_subscription_project",
                        message="project_id must be a non-empty UUID string",
                        file_path=manifest_path,
                        field_path=f"{current_path}.project_id",
                    )
                )
        if "workflow_id" in item and not str(item.get("workflow_id") or "").strip():
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_event_subscription_workflow",
                    message="workflow_id must be a non-empty string",
                    file_path=manifest_path,
                    field_path=f"{current_path}.workflow_id",
                )
            )


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value


def _normalize_host_context(
    context: dict | None,
    *,
    scope: ScopeContext | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    normalized_context = _as_dict(context)
    resolved_scope = normalize_scope(scope) if scope is not None else None

    host_scope: dict[str, Any] = {
        "account_id": (
            resolved_scope.account_id
            if resolved_scope is not None
            else str(normalized_context.get("account_id") or "").strip()
        ),
        "project_id": (
            resolved_scope.project_id
            if resolved_scope is not None and resolved_scope.project_id is not None
            else None
        ),
        "workflow_id": (
            resolved_scope.workflow_id
            if resolved_scope is not None and resolved_scope.workflow_id is not None
            else None
        ),
    }
    if resolved_scope is None:
        host_scope["project_id"] = str(normalized_context.get("project_id") or "").strip() or None
        workflow_id = str(normalized_context.get("workflow_id") or "").strip()
        host_scope["workflow_id"] = workflow_id or None

    principal: dict[str, Any] = {}
    if resolved_scope is not None:
        actor_id = str(resolved_scope.actor_id or "").strip()
        principal_type = str(resolved_scope.principal_type or "").strip()
        if actor_id:
            principal["actor_id"] = actor_id
        if principal_type:
            principal["principal_type"] = principal_type

    if host_scope.get("account_id"):
        normalized_context["account_id"] = host_scope["account_id"]
    if host_scope.get("project_id") is not None:
        normalized_context["project_id"] = host_scope["project_id"]
    if host_scope.get("workflow_id"):
        normalized_context["workflow_id"] = host_scope["workflow_id"]
    if principal.get("actor_id"):
        normalized_context.setdefault("actor_id", principal["actor_id"])
    if principal.get("principal_type"):
        normalized_context.setdefault("principal_type", principal["principal_type"])

    return normalized_context, host_scope, principal


def _event_type_allowed_for_extension(extension: dict, event_type: str) -> bool:
    declared = _normalize_string_list(
        extension.get("supported_events")
        or _as_dict(extension.get("compiled_manifest")).get("supported_events")
    )
    if not declared:
        return True
    normalized_event_type = str(event_type or "").strip()
    for pattern in declared:
        if pattern == normalized_event_type:
            return True
        if ("*" in pattern or "?" in pattern) and fnmatch.fnmatch(normalized_event_type, pattern):
            return True
    return False


def _issue(
    *,
    severity: str,
    message: str,
    code: str,
    file_path: str = "",
    field_path: str = "",
    hint: str = "",
) -> dict:
    return {
        "severity": str(severity or "error").strip().lower() or "error",
        "code": str(code or "validation_error").strip() or "validation_error",
        "message": str(message or "validation error").strip() or "validation error",
        "file_path": str(file_path or "").strip(),
        "field_path": str(field_path or "").strip(),
        "hint": str(hint or "").strip(),
    }


def _split_issues(issues: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    errors: list[dict] = []
    warnings: list[dict] = []
    for item in issues or []:
        payload = dict(item or {})
        if str(payload.get("severity") or "").strip().lower() == "warning":
            warnings.append(payload)
        else:
            errors.append(payload)
    return errors, warnings


def _forbidden_contract_json_files(bundle_dir: Path) -> list[Path]:
    matches: list[Path] = []
    for pattern in FORBIDDEN_EXTENSION_CONTRACT_JSON_PATTERNS:
        matches.extend(path for path in bundle_dir.glob(pattern) if path.is_file())
    return sorted(set(matches), key=lambda path: path.relative_to(bundle_dir).as_posix())


def _yaml_only_violation_issue(bundle_dir: Path, path: Path) -> dict:
    rel_path = path.relative_to(bundle_dir).as_posix()
    return _issue(
        severity="error",
        code="extension.yaml_only_violation",
        message=f"Extension contract files must be YAML-only; found {rel_path}",
        file_path=str(path),
        hint="Rename this extension contract file to YAML and update manifest references.",
    )


__all__ = [
    "_as_dict",
    "_as_list",
    "_normalize_string_list",
    "_normalize_subscription_severity",
    "_normalize_event_subscriptions",
    "_callback_definitions_value",
    "_workflow_trigger_definitions_value",
    "_validate_workflow_trigger_definitions",
    "_validate_callback_definitions",
    "_validate_event_subscription_list",
    "_json_clone",
    "_normalize_host_context",
    "_event_type_allowed_for_extension",
    "_issue",
    "_split_issues",
    "_forbidden_contract_json_files",
    "_yaml_only_violation_issue",
]
